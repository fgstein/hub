"""Configuration through environment variables (section 10)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .core.inventory import InventoryNotFoundError

ExecutorKind = Literal["fake", "local", "ssh"]


def _bool(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def inventory_search_path(env: Mapping[str, str] | None = None) -> list[Path]:
    """Places searched for the inventory when neither ``--inventory`` nor ``TRAINHUB_INVENTORY``
    is given: current directory, then XDG config directory, then ``/etc/trainhub``."""
    e = os.environ if env is None else env
    xdg = Path(e.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return [
        Path("inventory.yaml"),
        xdg / "trainhub" / "inventory.yaml",
        Path("/etc/trainhub/inventory.yaml"),
    ]


def find_inventory(
    explicit: str | Path | None = None, env: Mapping[str, str] | None = None
) -> Path:
    """Inventory path: ``explicit`` > ``TRAINHUB_INVENTORY`` > first existing file of
    :func:`inventory_search_path`. Falls back to ``./inventory.yaml`` when none exists."""
    e = os.environ if env is None else env
    if explicit:
        return Path(explicit)
    if e.get("TRAINHUB_INVENTORY"):
        return Path(e["TRAINHUB_INVENTORY"])
    candidates = inventory_search_path(e)
    return next((p for p in candidates if p.is_file()), candidates[0])


def require_inventory(path: Path) -> Path:
    """Return ``path`` if it exists, else raise an error listing where TrainHub looks."""
    if path.exists():
        return path
    raise InventoryNotFoundError(str(path), [str(p) for p in inventory_search_path()])


@dataclass(frozen=True)
class Settings:
    """Server settings, read from ``TRAINHUB_*``."""

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
            raise ValueError(f"invalid TRAINHUB_EXECUTOR: {executor!r} (fake, local or ssh)")
        static = e.get("TRAINHUB_STATIC_DIR")
        return cls(
            inventory=find_inventory(env=e),
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
