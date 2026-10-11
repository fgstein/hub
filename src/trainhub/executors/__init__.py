"""Executors: ``fake`` (simulation), ``local`` (local docker) and ``ssh`` (production)."""

from __future__ import annotations

from ..core.models import Inventory
from .base import (
    ACTION_TIMEOUT_S,
    LAUNCH_TIMEOUT_S,
    MONITOR_TIMEOUT_S,
    ExecResult,
    Executor,
    ExecutorError,
    LogStream,
    MachineUnreachableError,
)


def create_executor(kind: str, inventory: Inventory | None = None) -> Executor:
    """Instantiate the executor requested by ``TRAINHUB_EXECUTOR``."""
    if kind == "fake":
        from .fake import FakeExecutor

        return FakeExecutor(inventory.simulation if inventory else None, inventory=inventory)
    if kind == "local":
        from .local import LocalExecutor

        return LocalExecutor()
    if kind == "ssh":
        from .ssh import SshExecutor

        return SshExecutor()
    raise ValueError(f"unknown executor: {kind!r} (fake, local or ssh)")


__all__ = [
    "ACTION_TIMEOUT_S",
    "LAUNCH_TIMEOUT_S",
    "MONITOR_TIMEOUT_S",
    "ExecResult",
    "Executor",
    "ExecutorError",
    "LogStream",
    "MachineUnreachableError",
    "create_executor",
]
