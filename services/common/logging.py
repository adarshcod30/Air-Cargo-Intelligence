"""Console logging that keeps agent traces readable."""

from __future__ import annotations

import logging
import os
import sys

_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

_COLOURS = {
    "DEBUG": "\033[90m",
    "INFO": "\033[0m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
}
_RESET = "\033[0m"


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        colour = _COLOURS.get(record.levelname, "") if sys.stderr.isatty() else ""
        reset = _RESET if colour else ""
        name = record.name.replace("services.", "")
        return f"{colour}{record.levelname[:4]:<4} {name:<28} {record.getMessage()}{reset}"


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(_Formatter())
        log.addHandler(h)
        log.setLevel(_LEVEL)
        log.propagate = False
    return log
