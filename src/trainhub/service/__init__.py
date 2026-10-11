"""Couche service partagée par l'API, le WebSocket, le MCP, la classe TrainHub et la CLI."""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..executors import create_executor
from .events import Event, EventBus
from .inventory_watch import InventoryManager
from .runs import (
    ActionNotAllowedError,
    InvalidRequestError,
    NotFoundError,
    RunAction,
    RunService,
    ServiceError,
    UnreachableError,
    allowed_actions,
)
from .schemas import Selection, UiState
from .store import Store


def create_service(settings: Settings, *, version: str = "0.1.0") -> RunService:
    """Assemble inventaire, exécuteur et base SQLite selon la configuration."""
    inv = InventoryManager(settings.inventory)
    executor = create_executor(settings.executor, inv.inventory)
    store = Store(Path(settings.db_path))
    return RunService(inv, executor, store, poll_interval=settings.poll_interval_s, version=version)


__all__ = [
    "ActionNotAllowedError",
    "Event",
    "EventBus",
    "InvalidRequestError",
    "InventoryManager",
    "NotFoundError",
    "RunAction",
    "RunService",
    "Selection",
    "ServiceError",
    "Store",
    "UiState",
    "UnreachableError",
    "allowed_actions",
    "create_service",
]
