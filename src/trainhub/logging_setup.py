"""Logs structurés du serveur (une ligne JSON par événement)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Formate chaque enregistrement en JSON, avec les champs passés via ``extra``."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                data[k] = v
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, ensure_ascii=False)


def setup_logging(level: str = "INFO", *, json_format: bool = True) -> None:
    """Configure le logger ``trainhub`` (JSON sur stderr)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if json_format else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger("trainhub")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False
