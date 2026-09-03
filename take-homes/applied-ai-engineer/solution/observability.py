from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, TextIO


LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
SENSITIVE_KEY = re.compile(r"(?i)(authorization|api.?key|password|secret|token|credential)")
SENSITIVE_VALUE = re.compile(
    r"(?i)(?:\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+|\b(?:sk|xox[baprs])-[A-Za-z0-9_-]{8,})"
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_error(exc: BaseException | str, limit: int = 1000) -> str:
    value = str(exc).replace("\r", " ").replace("\n", " ")
    return SENSITIVE_VALUE.sub("[REDACTED]", value)[:limit]


def _sanitize(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return SENSITIVE_VALUE.sub("[REDACTED]", value)[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4000]


class EventLogger:
    def __init__(
        self,
        stream: TextIO | None = None,
        enabled: bool = True,
        *,
        level: str | None = None,
        service: str | None = None,
        environment: str | None = None,
        version: str | None = None,
    ):
        configured_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
        self.minimum_level = LEVELS.get(configured_level, LEVELS["INFO"])
        self.stream = stream or sys.stderr
        self.enabled = enabled
        self.context = {
            "service": service or os.getenv("SERVICE_NAME", "betterbark-intake"),
            "environment": environment or os.getenv("BETTERBARK_ENV", "development"),
            "version": version or os.getenv("SERVICE_VERSION", "dev"),
        }

    def emit(self, event: str, *, level: str = "INFO", **fields: Any) -> None:
        normalized_level = level.upper()
        if not self.enabled or LEVELS.get(normalized_level, 100) < self.minimum_level:
            return
        record = {
            "timestamp": utc_timestamp(),
            "level": normalized_level,
            "event": event,
            **self.context,
            **fields,
        }
        print(
            json.dumps(_sanitize(record), ensure_ascii=False, sort_keys=True),
            file=self.stream,
            flush=True,
        )
