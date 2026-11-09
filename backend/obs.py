"""obs.py - structured (JSON) logging with a request id.

One JSON line per event on stdout, so `kubectl logs` and Azure Container Insights can read it.
Set LOG_LEVEL (default INFO). Never log keys, passwords or the full question/answer text.
"""
import json
import logging
import os
import sys
from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar("request_id", default="-")


class _Json(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"), "level": r.levelname,
             "msg": r.getMessage(), "request_id": request_id.get()}
        d.update(getattr(r, "fields", {}))
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, default=str)


log = logging.getLogger("pharma")
if not log.handlers:                          # configure once
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(_Json())
    log.addHandler(_h)
    log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    log.propagate = False


def event(msg: str, level: int = logging.INFO, **fields) -> None:
    log.log(level, msg, extra={"fields": fields})
