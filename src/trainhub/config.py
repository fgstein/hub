"""Configuration par variables d'environnement (section 10)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ExecutorKind = Literal["fake", "local", "ssh"]


def _bool(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Réglages du serveur, lus depuis ``TRAINHUB_*``."""

    inventory: Path = Path("inventory.yaml")
    data_dir: Path = Path(".trainhub")
    host: str = "127.0.0.1"
    port: int = 8420
    executor: ExecutorKind = "ssh"
    token: str | None = None
    mcp_readonly: bool = False
    static_dir: Path | None = None
    poll_interval_s: float = 3.0
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trainhub.sqlite"

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        executor = e.get("TRAINHUB_EXECUTOR", "ssh").strip().lower()
        if executor not in ("fake", "local", "ssh"):
            raise ValueError(f"TRAINHUB_EXECUTOR invalide : {executor!r} (fake, local ou ssh)")
        static = e.get("TRAINHUB_STATIC_DIR")
        return cls(
            inventory=Path(e.get("TRAINHUB_INVENTORY", "inventory.yaml")),
            data_dir=Path(e.get("TRAINHUB_DATA_DIR", ".trainhub")),
            host=e.get("TRAINHUB_HOST", "127.0.0.1"),
            port=int(e.get("TRAINHUB_PORT", "8420")),
            executor=executor,  # type: ignore[arg-type]
            token=e.get("TRAINHUB_TOKEN") or None,
            mcp_readonly=_bool(e.get("TRAINHUB_MCP_READONLY")),
            static_dir=Path(static) if static else None,
            poll_interval_s=float(e.get("TRAINHUB_POLL_INTERVAL", "3")),
            log_level=e.get("TRAINHUB_LOG_LEVEL", "INFO").upper(),
        )
