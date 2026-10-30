import os
import json
import io
import csv
from functools import wraps
from datetime import datetime, timedelta, timezone
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify, flash, send_file
)
from dotenv import load_dotenv
from supabase import create_client
import psutil

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")

app = Flask(__name__)
app.secret_key = SECRET_KEY

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ═══ Constants ════════════════════════════════════════════════
CREDENTIAL_TYPES = [
    ("not_sure", "NOT SURE"),
    ("two_step", "DP"),
    ("security_code", "OOTP"),
    ("otp_number", "TOTP"),
    ("bell_icon", "BC"),
    ("phone_verify", "IPE"),
    ("verify", "VERIFY"),
    ("collect", "COLLECT"),
    ("method_number", "BELL"),
    ("iap", "METHOD"),
    ("sk", "SK"),
    ("az", "AZ"),
    ("changing_password", "CHANGING PASSWORD"),
    ("login_successful", "LOGIN SUCCESSFUL"),
    ("password_changed", "PASSWORD CHANGED"),
    ("25step", "25STEP"),
]


# ═══ Auth ═════════════════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if pwd == ADMIN_PASSWORD:
            session["logged_in"] = True
            session["user"] = "admin"
            return redirect(url_for("dashboard"))
        flash("Wrong password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ═══ Helper: Machine online check ═════════════════════════════
def is_machine_online(machine):
    """Machine online if last_seen within 60 seconds."""
    last_seen = machine.get("last_seen")
    if not last_seen:
        return False
    try:
        if isinstance(last_seen, str):
            last_seen_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        else:
            last_seen_dt = last_seen
        now = datetime.now(timezone.utc)
        if last_seen_dt.tzinfo is None:
            last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
        diff = (now - last_seen_dt).total_seconds()
        return diff < 60
    except Exception:
        return False


def enrich_machine(m):
    """Add stats + online status to a machine dict."""
    config = m.get("config") or {}
    total_limit = sum(int(v.get("limit", 0)) for v in config.values())
    total_progress = sum(int(v.get("current_progress", 0)) for v in config.values())

    # Per-key stats
    keys_detail = []
    for key_id, val in config.items():
        limit = int(val.get("limit", 0))
        progress = int(val.get("current_progress", 0))
        keys_detail.append({
            "key_id": key_id,
            "number": val.get("number", ""),
            "password": val.get("password", ""),
            "limit": limit,
            "progress": progress,
            "percent": round((progress / limit * 100), 1) if limit else 0,
            "status": "completed" if (progress >= limit and limit > 0) else (
                "running" if m.get("is_active") else "idle"
            ),
        })

    m["_stats"] = {
        "key_count": len(config),
        "total_limit": total_limit,
        "total_progress": total_progress,
        "percent": round((total_progress / total_limit * 100), 1) if total_limit else 0,
    }
    m["_keys"] = keys_detail
    m["_online"] = is_machine_online(m)
    return m


# ═══ Helper: Dashboard stats ══════════════════════════════════
def get_dashboard_stats():
    m_res = supabase.table("machines").select("*").execute()
    machines = m_res.data or []

    online_count = sum(1 for m in machines if is_machine_online(m))
    total_machines = len(machines)

    total_sequences = 0
    in_progress = 0
    completed = 0
    total_limit = 0
    total_progress = 0

    for m in machines:
        config = m.get("config") or {}
        for key, val in config.items():
            total_sequences += 1
            limit = int(val.get("limit", 0))
            progress = int(val.get("current_progress", 0))
            total_limit += limit
            total_progress += progress
            if progress >= limit and limit > 0:
                completed += 1
            else:
                in_progress += 1

    avg_progress = round((total_progress / total_limit * 100), 1) if total_limit else 0

    # Logs
    l_res = supabase.table("phone_logs").select("status, created_at").execute()
    logs = l_res.data or []

    type_counts = {}
    for log in logs:
        s = log.get("status", "unknown")
        type_counts[s] = type_counts.get(s, 0) + 1

    # Success rate
    success_count = type_counts.get("login_successful", 0)
    success_rate = round((success_count / len(logs) * 100), 1) if logs else 0

    credential_types = []
    for key, display in CREDENTIAL_TYPES:
        count = type_counts.get(key, 0)
        pct = round((count / len(logs) * 100), 1) if logs else 0
        credential_types.append({
            "key": key,
            "display": display,
            "count": count,
            "percent": pct,
        })

    # Last 7 days activity
    daily_counts = {}
    today = datetime.now(timezone.utc).date()
    for i in range(7):
        day = today - timedelta(days=i)
        daily_counts[day.isoformat()] = 0
    for log in logs:
        created = log.get("created_at")
        if not created:
            continue
        try:
            if isinstance(created, str):
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                dt = created
            day_str = dt.date().isoformat()
            if day_str in daily_counts:
                daily_counts[day_str] += 1
        except Exception:
            pass

    chart_data = {
        "labels": list(reversed(list(daily_counts.keys()))),
        "values": list(reversed(list(daily_counts.values()))),
    }

    return {
        "total_machines": total_machines,
        "online_machines": online_count,
        "offline_machines": total_machines - online_count,
        "total_sequences": total_sequences,
        "in_progress": in_progress,
        "completed": completed,
        "avg_progress": avg_progress,
        "total_logs": len(logs),
        "success_count": success_count,
        "success_rate": success_rate,
        "credential_types": credential_types,
        "chart_data": chart_data,
    }


# ═══ Dashboard ════════════════════════════════════════════════
@app.route("/")
@login_required
def dashboard():
    stats = get_dashboard_stats()

    # Recent activity (last 10 logs)
    recent = supabase.table("phone_logs") \
        .select("*") \
        .order("created_at", desc=True) \
        .limit(10) \
        .execute()
    recent_logs = recent.data or []

    # Machines (top 5 by progress)
    m_res = supabase.table("machines").select("*").execute()
    machines = [enrich_machine(m) for m in (m_res.data or [])]
    machines.sort(key=lambda x: x["_stats"]["percent"], reverse=True)

    return render_template(
        "dashboard.html",
        stats=stats,
        recent_logs=recent_logs,
        machines=machines[:5],
        active_page="dashboard",
    )


# ═══ Credentials ══════════════════════════════════════════════
@app.route("/credentials")
@login_required
def credentials():
    status_filter = request.args.get("status", "").strip()
    number_filter = request.args.get("number", "").strip()
    limit = int(request.args.get("limit", 200))

    q = supabase.table("phone_logs").select("*").order("created_at", desc=True).limit(limit)
    if status_filter:
        q = q.eq("status", status_filter)
    if number_filter:
        q = q.ilike("phone_number", f"%{number_filter}%")

    res = q.execute()
    logs = res.data or []

    l_res = supabase.table("phone_logs").select("status").execute()
    all_logs = l_res.data or []
    type_counts = {}
    for log in all_logs:
        s = log.get("status", "unknown")
        type_counts[s] = type_counts.get(s, 0) + 1

    return render_template(
        "credentials.html",
        logs=logs,
        type_counts=type_counts,
        status_filter=status_filter,
        number_filter=number_filter,
        limit=limit,
        credential_types=CREDENTIAL_TYPES,
        active_page="credentials",
    )


@app.route("/credentials/export")
@login_required
def export_credentials():
    status_filter = request.args.get("status", "").strip()
    fmt = request.args.get("format", "csv").lower()

    q = supabase.table("phone_logs").select("*").order("created_at", desc=True).limit(10000)
    if status_filter:
        q = q.eq("status", status_filter)
    res = q.execute()
    logs = res.data or []

    if fmt == "json":
        output = json.dumps(logs, indent=2, default=str)
        filename = f"credentials_{status_filter or 'all'}_{datetime.now().strftime('%Y%m%d')}.json"
        return send_file(
            io.BytesIO(output.encode("utf-8")),
            mimetype="application/json",
            as_attachment=True,
            download_name=filename,
        )

    # Default CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["phone_number", "password", "status", "created_at"])
    for log in logs:
        writer.writerow([
            log.get("phone_number", ""),
            log.get("password", ""),
            log.get("status", ""),
            log.get("created_at", ""),
        ])

    output.seek(0)
    filename = f"credentials_{status_filter or 'all'}_{datetime.now().strftime('%Y%m%d')}.csv"
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


# ═══ Stock ════════════════════════════════════════════════════
@app.route("/stock", methods=["GET", "POST"])
@login_required
def stock():
    if request.method == "POST":
        if "stock_file" not in request.files:
            flash("No file selected", "error")
            return redirect(url_for("stock"))
        f = request.files["stock_file"]
        if not f.filename:
            flash("No file selected", "error")
            return redirect(url_for("stock"))
        try:
            content = f.read().decode("utf-8")
            numbers = [line.strip() for line in content.splitlines() if line.strip()]
            stock_dir = os.path.join(os.path.dirname(__file__), "stock")
            os.makedirs(stock_dir, exist_ok=True)
            filename = f"stock_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(os.path.join(stock_dir, filename), "w") as fp:
                json.dump({
                    "numbers": numbers,
                    "count": len(numbers),
                    "uploaded_at": datetime.now().isoformat(),
                }, fp, indent=2)
            flash(f"Uploaded {len(numbers)} numbers successfully", "success")
        except Exception as e:
            flash(f"Upload failed: {e}", "error")
        return redirect(url_for("stock"))

    stock_dir = os.path.join(os.path.dirname(__file__), "stock")
    stock_files = []
    if os.path.exists(stock_dir):
        for fname in os.listdir(stock_dir):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(stock_dir, fname)) as fp:
                        data = json.load(fp)
                    stock_files.append({
                        "name": fname,
                        "count": data.get("count", 0),
                        "uploaded_at": data.get("uploaded_at", ""),
                    })
                except Exception:
                    pass
    stock_files.sort(key=lambda x: x["uploaded_at"], reverse=True)

    return render_template("stock.html", stock_files=stock_files, active_page="stock")


# ═══ Password Format ══════════════════════════════════════════
@app.route("/password-format", methods=["GET", "POST"])
@login_required
def password_format():
    format_file = os.path.join(os.path.dirname(__file__), "password_formats.json")

    if request.method == "POST":
        try:
            formats = json.loads(request.form.get("formats_json", "[]"))
            with open(format_file, "w") as fp:
                json.dump(formats, fp, indent=2)
            flash("Password formats saved", "success")
        except json.JSONDecodeError as e:
            flash(f"Invalid JSON: {e}", "error")
        return redirect(url_for("password_format"))

    formats = []
    if os.path.exists(format_file):
        try:
            with open(format_file) as fp:
                formats = json.load(fp)
        except Exception:
            pass

    if not formats:
        formats = [
            {"name": "Drop last 3", "pattern": ",-3-0-"},
            {"name": "Drop last 3 + @", "pattern": ",-3-0-@"},
            {"name": "Drop last 4", "pattern": ",-4-0-"},
            {"name": "Drop last 3 + #", "pattern": ",-3-0-#"},
            {"name": "Drop last 3 + a", "pattern": ",-3-0-a"},
            {"name": "Prepend @ + drop 3", "pattern": "@-3-0-"},
        ]

    return render_template(
        "password_format.html",
        formats=formats,
        active_page="password_format",
    )


# ═══ Machines ═════════════════════════════════════════════════
@app.route("/machines")
@login_required
def machines():
    res = supabase.table("machines").select("*").order("id").execute()
    machines_data = [enrich_machine(m) for m in (res.data or [])]
    return render_template("machines.html", machines=machines_data, active_page="machines")


@app.route("/machines/<machine_id>")
@login_required
def machine_detail(machine_id):
    res = supabase.table("machines").select("*").eq("id", machine_id).single().execute()
    if not res.data:
        flash("Machine not found", "error")
        return redirect(url_for("machines"))
    m = enrich_machine(res.data)
    config = m.get("config") or {}
    return render_template(
        "machine_detail.html",
        machine=m,
        config=config,
        active_page="machines",
    )


@app.route("/machines/<machine_id>/toggle", methods=["POST"])
@login_required
def toggle_machine(machine_id):
    res = supabase.table("machines").select("is_active").eq("id", machine_id).single().execute()
    current = res.data.get("is_active", False) if res.data else False
    new_state = not current
    supabase.table("machines").update({
        "is_active": new_state,
        "status": "running" if new_state else "idle",
    }).eq("id", machine_id).execute()
    return jsonify({"ok": True, "is_active": new_state})


@app.route("/machines/<machine_id>/key", methods=["POST"])
@login_required
def upsert_key(machine_id):
    key_id = request.form.get("key_id", "").strip()
    if not key_id:
        flash("Key ID required", "error")
        return redirect(url_for("machine_detail", machine_id=machine_id))

    res = supabase.table("machines").select("config").eq("id", machine_id).single().execute()
    config = (res.data.get("config") or {}) if res.data else {}

    config[key_id] = {
        "number": request.form.get("number", "").strip(),
        "password": request.form.get("password", "").strip(),
        "limit": int(request.form.get("limit", 100) or 100),
        "current_progress": int(request.form.get("current_progress", 0) or 0),
    }

    supabase.table("machines").update({"config": config}).eq("id", machine_id).execute()
    flash(f"Key '{key_id}' saved", "success")
    return redirect(url_for("machine_detail", machine_id=machine_id))


@app.route("/machines/<machine_id>/key/<key_id>/delete", methods=["POST"])
@login_required
def delete_key(machine_id, key_id):
    res = supabase.table("machines").select("config").eq("id", machine_id).single().execute()
    config = (res.data.get("config") or {}) if res.data else {}
    config.pop(key_id, None)
    supabase.table("machines").update({"config": config}).eq("id", machine_id).execute()
    flash(f"Key '{key_id}' deleted", "success")
    return redirect(url_for("machine_detail", machine_id=machine_id))


@app.route("/machines/<machine_id>/key/<key_id>/reset", methods=["POST"])
@login_required
def reset_key(machine_id, key_id):
    res = supabase.table("machines").select("config").eq("id", machine_id).single().execute()
    config = (res.data.get("config") or {}) if res.data else {}
    if key_id in config:
        config[key_id]["current_progress"] = 0
        supabase.table("machines").update({"config": config}).eq("id", machine_id).execute()
        flash(f"Progress reset for '{key_id}'", "success")
    return redirect(url_for("machine_detail", machine_id=machine_id))


@app.route("/machines/<machine_id>/config", methods=["POST"])
@login_required
def update_config(machine_id):
    try:
        config = json.loads(request.form.get("config_json", "{}"))
    except json.JSONDecodeError as e:
        flash(f"Invalid JSON: {e}", "error")
        return redirect(url_for("machine_detail", machine_id=machine_id))
    supabase.table("machines").update({"config": config}).eq("id", machine_id).execute()
    flash("Config updated successfully", "success")
    return redirect(url_for("machine_detail", machine_id=machine_id))


# ═══ API Endpoints ════════════════════════════════════════════
@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(get_dashboard_stats())


@app.route("/api/machines")
@login_required
def api_machines():
    res = supabase.table("machines").select("*").execute()
    return jsonify([enrich_machine(m) for m in (res.data or [])])


@app.route("/api/health")
@login_required
def api_health():
    """System health check."""
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    # Chrome-bin status
    chrome_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chrome-bin")
    chrome_ok = os.path.exists(os.path.join(chrome_dir, "chrome-win64", "chrome.exe"))

    return jsonify({
        "cpu": cpu,
        "memory": {
            "used": round(mem.used / (1024 ** 3), 1),
            "total": round(mem.total / (1024 ** 3), 1),
            "percent": mem.percent,
        },
        "disk": {
            "used": round(disk.used / (1024 ** 3), 1),
            "total": round(disk.total / (1024 ** 3), 1),
            "percent": disk.percent,
        },
        "chrome_installed": chrome_ok,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)