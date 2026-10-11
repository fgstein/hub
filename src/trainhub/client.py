""":class:`TrainHub` class: using TrainHub from plain Python, without the web server.

Example ::

    from trainhub import TrainHub

    tb = TrainHub.from_inventory("inventory.yaml")
    run = await tb.launch(project="segmentation", machine="gpu-01",
                          image="torch-2.4", venv="main", config="baseline")
    async for chunk in tb.stream_logs(run.id):
        print(chunk.decode(), end="")
    await tb.stop(run.id)
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from types import TracebackType

from .config import Settings, find_inventory, require_inventory
from .core.models import Run, RunSource, RunStatus
from .executors import Executor, create_executor
from .service import InventoryManager, RunService, Selection, Store
from .service.schemas import ConfigList, PreviewResult


class TrainHub:
    """Asynchronous facade around :class:`~trainhub.service.RunService`.

    The service starts lazily on the first ``await`` call (SQLite database, machine
    monitoring). Usable as an asynchronous context manager.
    """

    def __init__(
        self, service: RunService, *, monitor: bool = True, source: RunSource = RunSource.CLI
    ) -> None:
        self.service = service
        self._monitor = monitor
        self._source = source

    @classmethod
    def from_inventory(
        cls,
        path: str | Path | None = None,
        *,
        executor: str | Executor | None = None,
        data_dir: str | Path | None = None,
        poll_interval: float = 3.0,
        monitor: bool = True,
    ) -> TrainHub:
        """Create an instance from an inventory file.

        ``path``: defaults to ``TRAINHUB_INVENTORY``, else the first of ``./inventory.yaml``,
        ``~/.config/trainhub/inventory.yaml`` and ``/etc/trainhub/inventory.yaml`` that exists.

        ``executor``: ``"fake"``, ``"local"``, ``"ssh"`` or an instance; defaults to
        ``TRAINHUB_EXECUTOR`` (else ``ssh``). ``data_dir``: SQLite database directory
        (default ``TRAINHUB_DATA_DIR`` or ``.trainhub``); ``":memory:"`` for a throwaway database.
        """
        inv = InventoryManager(require_inventory(find_inventory(path)))
        if executor is None or isinstance(executor, str):
            kind = executor or os.environ.get("TRAINHUB_EXECUTOR", "ssh")
            ex = create_executor(kind, inv.inventory)
        else:
            ex = executor
        if data_dir == ":memory:":
            store = Store(":memory:")
        else:
            d = Path(data_dir) if data_dir else Settings.from_env().data_dir
            store = Store(d / "trainhub.sqlite")
        return cls(RunService(inv, ex, store, poll_interval=poll_interval), monitor=monitor)

    async def start(self) -> None:
        await self.service.start(monitor=self._monitor)

    async def close(self) -> None:
        await self.service.close()

    async def __aenter__(self) -> TrainHub:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # -- actions -----------------------------------------------------------------

    async def preview(
        self,
        *,
        project: str,
        machine: str,
        image: str,
        venv: str,
        config: str,
        overrides: Sequence[str] = (),
        cuda_visible_devices: str | None = None,
        params: Mapping[str, str] | None = None,
    ) -> PreviewResult:
        await self.start()
        return await self.service.preview(
            Selection(
                project=project,
                machine=machine,
                image=image,
                venv=venv,
                config=config,
                overrides=list(overrides),
                cuda_visible_devices=cuda_visible_devices,
                params=dict(params or {}),
            )
        )

    async def launch(
        self,
        *,
        project: str,
        machine: str,
        image: str,
        venv: str,
        config: str,
        overrides: Sequence[str] = (),
        cuda_visible_devices: str | None = None,
        params: Mapping[str, str] | None = None,
        wait: bool | float = True,
    ) -> Run:
        """Launch a run; by default waits for ``docker run -d`` to finish."""
        await self.start()
        sel = Selection(
            project=project,
            machine=machine,
            image=image,
            venv=venv,
            config=config,
            overrides=list(overrides),
            cuda_visible_devices=cuda_visible_devices,
            params=dict(params or {}),
        )
        return await self.service.launch(sel, source=self._source, wait=wait)

    async def get(self, run_id: str) -> Run:
        await self.start()
        return await self.service.get_run(run_id)

    async def runs(
        self, *, project: str | None = None, status: RunStatus | None = None
    ) -> list[Run]:
        await self.start()
        return await self.service.list_runs(project=project, status=status)

    async def configs(self, project: str, machine: str | None = None) -> ConfigList:
        await self.start()
        return await self.service.get_configs(project, machine)

    async def pause(self, run_id: str) -> Run:
        await self.start()
        return await self.service.pause(run_id)

    async def resume(self, run_id: str) -> Run:
        await self.start()
        return await self.service.resume(run_id)

    async def stop(self, run_id: str, *, wait: bool | float = True) -> Run:
        await self.start()
        return await self.service.stop(run_id, wait=wait)

    async def restart(self, run_id: str) -> Run:
        await self.start()
        return await self.service.restart(run_id)

    async def relaunch(self, run_id: str, *, wait: bool | float = True) -> Run:
        await self.start()
        return await self.service.relaunch(run_id, source=self._source, wait=wait)

    async def delete(self, run_id: str) -> None:
        await self.start()
        await self.service.delete(run_id)

    async def stream_logs(self, run_id: str, *, follow: bool = True) -> AsyncIterator[bytes]:
        """Raw logs: buffer then live until the run ends (``follow=False``: existing ones)."""
        await self.start()
        if not follow:
            yield await self.service.read_logs(run_id)
            return
        async for chunk in self.service.stream_logs(run_id):
            yield chunk

    async def logs(self, run_id: str, tail: int | None = None) -> bytes:
        await self.start()
        return await self.service.read_logs(run_id, tail)
