"""
JSON file logging for FintNet, Splunk-ready.

Writes one JSON event per line to logs/fintnet.json (rotated at 10 MB, keep 5).
Use `log.info("connect.start", extra={"bank": "ing", "user_id": 7})` — every
field becomes a Splunk-searchable property without regex parsing.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from pythonjsonlogger.json import JsonFormatter

# The project directory is read-only on serverless hosts (Vercel et al.)
# — only /tmp is writable — so the log file has to live there. LOG_DIR
# overrides both. The console handler always runs; the file handler is
# best-effort and silently skipped if its directory can't be written.
LOG_DIR  = os.getenv("LOG_DIR") or ("/tmp/logs" if os.getenv("VERCEL") else "logs")
LOG_FILE = os.path.join(LOG_DIR, "fintnet.json")


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    formatter = JsonFormatter(
        "{asctime} {levelname} {name} {message}",
        style="{",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    handlers: list[logging.Handler] = [console_handler]

    # Best-effort file logging — on a read-only FS (e.g. Vercel without a
    # writable LOG_DIR) we just keep the console handler so import never
    # crashes. On Vercel, stdout/stderr is captured in the function logs.
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        handlers.insert(0, file_handler)
    except OSError:
        pass

    root = logging.getLogger("fintnet")
    root.setLevel(level)
    root.handlers = handlers
    root.propagate = False
    return root


log = setup_logging()
