import logging
import os
from runtime_paths import BASE_DIR

def setup_logging(log_filename="app.log", name="app"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        log_path = os.path.join(BASE_DIR, "logs")
        os.makedirs(log_path, exist_ok=True)

        fh = logging.FileHandler(os.path.join(log_path, log_filename), encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(ch)

    return logger