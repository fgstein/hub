from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from trainhub.core.inventory import parse_inventory
from trainhub.executors.fake import FakeExecutor
from trainhub.service import InventoryManager, RunService, Selection, Store

from .conftest import BASIC_INVENTORY

SIMULATION = """
simulation:
  duration_s: [{duration}, {duration}]
  time_scale: {scale}
  latency_ms: [0, 1]
  failure_prob: {failure}
  log_rate: 20
  seed: 1
  configs:
    detection: [yolo_s, yolo_m]
"""


def sim_inventory_text(duration: float = 40, scale: float = 20, failure: float = 0.0) -> str:
    return BASIC_INVENTORY + SIMULATION.format(duration=duration, scale=scale, failure=failure)


def make_service(
    tmp_path: Path | None = None,
    *,
    executor: FakeExecutor | None = None,
    text: str | None = None,
    db: str | None = None,
    **sim: float,
) -> RunService:
    inv = InventoryManager(text=text or sim_inventory_text(**sim))
    ex = executor or FakeExecutor(inv.inventory.simulation, inventory=inv.inventory)
    path = db or (str(tmp_path / "th.sqlite") if tmp_path else ":memory:")
    return RunService(inv, ex, Store(path), poll_interval=0.05, log_idle_timeout=0.2)


SEL = Selection(
    project="segmentation", machine="gpu-01", image="torch-2.4", venv="main", config="baseline"
)


async def wait_for(
    pred: Callable[[], Awaitable[bool]], timeout: float = 10.0, interval: float = 0.05
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await pred():
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition non atteinte dans le délai")
        await asyncio.sleep(interval)


__all__ = ["SEL", "make_service", "parse_inventory", "sim_inventory_text", "wait_for"]
