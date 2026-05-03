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

LOG_DIR  = "logs"
LOG_FILE = os.path.join(LOG_DIR, "fintnet.json")


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = JsonFormatter(
        "{asctime} {levelname} {name} {message}",
        style="{",
        rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    root = logging.getLogger("fintnet")
    root.setLevel(level)
    root.handlers = [file_handler, console_handler]
    root.propagate = False
    return root


log = setup_logging()
