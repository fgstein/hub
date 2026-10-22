"""Service layer: run lifecycle, persistence, reconciliation, event bus.

This is the single entry point of the business logic: the REST API, the WebSocket, the MCP
server, the :class:`trainhub.TrainHub` class and the CLI all call it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from .. import __version__
from ..core import commands as cmd
from ..core import gpus, load
from ..core.commands import RunSpec
from ..core.ids import new_run_id
from ..core.logs_clean import clean_logs
from ..core.models import (
    FINISHED_STATUSES,
    ConfigDiscover,
    Inventory,
    Machine,
    MlflowInfo,
    Project,
    Run,
    RunSource,
    RunStatus,
    Series,
    UnknownResourceError,
    command_params,
    validate_command_template,
)
from ..core.selection import SelectionError, make_spec
from ..core.series import SeriesTracker
from ..core.status import ContainerState, parse_inspect, status_from_container, utcnow
from ..executors.base import (
    ACTION_TIMEOUT_S,
    LAUNCH_TIMEOUT_S,
    ExecResult,
    Executor,
    ExecutorError,
    LogStream,
    MachineUnreachableError,
    ShellSession,
)
from .events import EventBus
from .inventory_watch import InventoryManager
from .log_hub import LogHub, LogsGoneError, LogSubscriber
from .mlflow import HttpMlflow, MlflowSource, MlflowTracker
from .schemas import (
    ConfigList,
    CpuView,
    GpuRunView,
    GpuView,
    Health,
    ImageView,
    InventoryView,
    LastSelection,
    MachineGpus,
    MachineLoad,
    MachineStatus,
    MachineView,
    PreviewResult,
    ProjectView,
    RunView,
    Selection,
    SeriesRequest,
    Snapshot,
    UiState,
    VenvView,
)
from .store import Store

log = logging.getLogger("trainhub.service")

CONFIG_CACHE_TTL_S = 60.0
UI_STATE_KEY = "ui_state"
#: Polling period of :meth:`RunService.wait_run` (capped by the monitoring period).
WAIT_POLL_S = 1.0
#: Unfinished last line kept by :meth:`RunService.wait_run` between two reads.
_CARRY_MAX = 64 * 1024

WaitUntil = Literal["finished", "status_change"]
WaitReason = Literal["finished", "match", "status_change", "timeout"]


@dataclass(frozen=True)
class LogSlice:
    """Part of a run's logs, read from a byte offset."""

    data: bytes
    start: int
    """Offset of ``data[0]`` in the full logs."""
    cursor: int
    """Offset to pass next time to only get what follows."""
    reset: bool
    """The logs were rewritten (shorter than the requested offset): read from the start."""


@dataclass(frozen=True)
class WaitResult:
    run: Run
    reason: WaitReason
    waited_s: float
    match: str | None = None
    """Matching log line (cleaned) when a pattern was given."""
    cursor: int | None = None
    """Log offset reached by the search (``None`` without a pattern)."""


# --------------------------------------------------------------------------- errors


class ServiceError(Exception):
    """Business error, shown as is to the user."""

    status_code = 400

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(ServiceError):
    status_code = 404


class ActionNotAllowedError(ServiceError):
    status_code = 409


class UnreachableError(ServiceError):
    status_code = 409


class InvalidRequestError(ServiceError):
    status_code = 422


class RunAction(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    RESTART = "restart"
    RELAUNCH = "relaunch"
    CLOSE = "close"


#: Allowed starting statuses for each action (section 4.2).
ALLOWED_FROM: dict[str, frozenset[RunStatus]] = {
    RunAction.PAUSE: frozenset({RunStatus.RUNNING}),
    RunAction.RESUME: frozenset({RunStatus.PAUSED}),
    RunAction.STOP: frozenset({RunStatus.RUNNING, RunStatus.PAUSED}),
    RunAction.RESTART: FINISHED_STATUSES,
    RunAction.RELAUNCH: FINISHED_STATUSES,
    RunAction.CLOSE: FINISHED_STATUSES,
    "delete": FINISHED_STATUSES,
}

_ACTION_LABELS = {
    RunAction.PAUSE: "pause",
    RunAction.RESUME: "resume",
    RunAction.STOP: "stop",
    RunAction.RESTART: "restart",
    RunAction.RELAUNCH: "relaunch",
    RunAction.CLOSE: "close the container of",
    "delete": "delete",
}


def allowed_actions(status: RunStatus, *, container_alive: bool = False) -> list[str]:
    """Possible actions for a given status (``close``: kept container still started)."""
    return [
        str(a)
        for a, statuses in ALLOWED_FROM.items()
        if status in statuses and (a != RunAction.CLOSE or container_alive)
    ]


# --------------------------------------------------------------------------- service


class RunService:
    """TrainHub business facade."""

    def __init__(
        self,
        inventory: InventoryManager,
        executor: Executor,
        store: Store,
        *,
        bus: EventBus | None = None,
        poll_interval: float = 3.0,
        log_idle_timeout: float = 30.0,
        log_dir: Path | None = None,
        version: str = __version__,
        mlflow: MlflowSource | None = None,
    ) -> None:
        self.inv_mgr = inventory
        self.executor = executor
        self.store = store
        self.bus = bus or EventBus()
        self.poll_interval = poll_interval
        self.version = version
        self.logs = LogHub(
            self._open_log_stream,
            self._is_finished,
            log_dir=log_dir,
            idle_timeout=log_idle_timeout,
            finish_grace=min(1.5, log_idle_timeout),
            observer=self._observe_logs,
        )
        self._series_trackers: dict[str, SeriesTracker] = {}
        self._series_lock = asyncio.Lock()
        self.machine_status: dict[str, MachineStatus] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._config_cache: dict[
            tuple[str, str], tuple[float, datetime, list[str] | None, str | None]
        ] = {}
        self._project_commands: dict[str, str] = {}
        self._started = False
        from .monitor import Monitor

        self.monitor = Monitor(self, poll_interval)
        if mlflow is None:
            mlflow = executor.mlflow_source() or HttpMlflow()
        self.mlflow = MlflowTracker(self, mlflow)
        self.executor.update_inventory(self.inventory)
        self.inv_mgr.on_change(self._on_inventory_change)

    # -- lifecycle ---------------------------------------------------------------------

    @property
    def inventory(self) -> Inventory:
        return self.inv_mgr.inventory

    async def start(self, *, monitor: bool = True, watch_inventory: bool = True) -> None:
        """Open the database, start machine monitoring and inventory watching."""
        if self._started:
            return
        self._started = True
        if self.store._db is None:
            await self.store.open()
        self._project_commands = await self.store.get_project_commands()
        if watch_inventory:
            self.inv_mgr.start()
        if monitor:
            self.monitor.start()
            self.mlflow.start()

    async def close(self) -> None:
        await self.monitor.stop()
        await self.mlflow.stop()
        await self.inv_mgr.stop()
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.logs.close()
        await self.executor.close()
        await self.store.close()
        self._started = False

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _on_inventory_change(self, inv: Inventory) -> None:
        self.executor.update_inventory(inv)
        self._config_cache.clear()
        self.monitor.sync_machines()
        self.bus.publish(
            "inventory.updated", {"inventory": self.inventory_view().model_dump(mode="json")}
        )

    # -- inventory and projects ---------------------------------------------------------

    def _project(self, project_id: str) -> Project:
        try:
            return self.inventory.project(project_id)
        except UnknownResourceError as exc:
            raise NotFoundError(str(exc)) from exc

    def _machine(self, machine_id: str) -> Machine:
        try:
            return self.inventory.machine(machine_id)
        except UnknownResourceError as exc:
            raise NotFoundError(str(exc)) from exc

    def project_command(self, project_id: str) -> str:
        """Effective generic command (persisted override, else YAML)."""
        return self._project_commands.get(project_id) or self._project(project_id).command

    def project_view(self, p: Project) -> ProjectView:
        inv = self.inventory
        venvs = inv.allowed_venvs(p)
        override = self._project_commands.get(p.id)
        return ProjectView(
            id=p.id,
            label=p.display,
            command=override or p.command,
            yaml_command=p.command,
            command_overridden=override is not None and override != p.command,
            params=command_params(override or p.command),
            workdir=p.workdir,
            machines=[m.id for m in inv.allowed_machines(p)],
            images=[i.id for i in inv.allowed_images(p)],
            venvs=[v.id for v in venvs],
            venv_images={v.id: v.images for v in venvs},
            static_configs=p.configs.static,
            discover=p.configs.discover is not None,
            docker=p.docker,
        )

    def machine_state(self, machine_id: str) -> MachineStatus:
        st = self.machine_status.get(machine_id)
        if st is None:
            st = self.machine_status[machine_id] = MachineStatus(id=machine_id)
        return st

    def inventory_view(self) -> InventoryView:
        inv = self.inventory
        return InventoryView(
            machines=[
                MachineView(
                    id=m.id,
                    label=m.label,
                    host=m.host,
                    user=m.user,
                    port=m.port,
                    status=self.machine_state(m.id),
                )
                for m in inv.machines
            ],
            images=[ImageView(id=i.id, image=i.image, label=i.label) for i in inv.images],
            venvs=[
                VenvView(id=v.id, path=v.path, label=v.label, images=v.images) for v in inv.venvs
            ],
            projects=[self.project_view(p) for p in inv.projects],
            error=self.inv_mgr.error,
            executor=self.executor.kind,
        )

    async def set_project_command(self, project_id: str, command: str | None) -> ProjectView:
        """Override the project command (``None``: back to the YAML value)."""
        p = self._project(project_id)
        if command is not None:
            command = command.strip()
            try:
                validate_command_template(command)
            except ValueError as exc:
                raise InvalidRequestError(f"invalid command: {exc}") from exc
            if command == p.command:
                command = None
        await self.store.set_project_command(project_id, command)
        if command is None:
            self._project_commands.pop(project_id, None)
        else:
            self._project_commands[project_id] = command
        view = self.project_view(p)
        self.bus.publish(
            "inventory.updated", {"inventory": self.inventory_view().model_dump(mode="json")}
        )
        return view

    async def get_configs(
        self, project_id: str, machine_id: str | None = None, *, refresh: bool = False
    ) -> ConfigList:
        """Available configs: static list and/or discovery (60 s cache)."""
        p = self._project(project_id)
        static = list(p.configs.static or [])
        discovered: list[str] | None = None
        error: str | None = None
        cached_at: datetime | None = None
        d = p.configs.discover
        if d is not None:
            if machine_id is None:
                allowed = self.inventory.allowed_machines(p)
                machine_id = allowed[0].id if allowed else None
            if machine_id is not None:
                machine = self._machine(machine_id)
                key = (project_id, machine_id)
                hit = self._config_cache.get(key)
                if hit is None or refresh or time.monotonic() - hit[0] > CONFIG_CACHE_TTL_S:
                    found, err = await self._discover(machine, d)
                    hit = (time.monotonic(), utcnow(), found, err)
                    self._config_cache[key] = hit
                _, cached_at, discovered, error = hit
        configs = list(dict.fromkeys(static + (discovered or [])))
        counts = await self.store.count_runs_by_config(project_id)
        return ConfigList(
            project=project_id,
            machine=machine_id,
            configs=configs,
            root=d.path if d is not None else None,
            run_counts={c: counts[c] for c in configs if c in counts},
            static=static,
            discovered=discovered,
            discover_error=error,
            cached_at=cached_at,
        )

    async def _discover(
        self, machine: Machine, discover: ConfigDiscover
    ) -> tuple[list[str] | None, str | None]:
        try:
            res = await self.executor.exec(machine, cmd.list_configs(discover), timeout=20.0)
        except (MachineUnreachableError, ExecutorError) as exc:
            return None, str(exc)
        names = cmd.parse_config_listing(res.out, discover)
        # find returns a non-zero code if a subdirectory is unreadable: keep what was
        # found and only report an error if nothing could be listed.
        if not res.ok and not names:
            return None, f"config discovery failed: {res.error_message()}"
        return names, None

    # -- UI state --------------------------------------------------------------------

    async def get_ui_state(self) -> UiState:
        raw = await self.store.get_kv(UI_STATE_KEY)
        projects = [p.id for p in self.inventory.projects]
        if raw is None:
            state = UiState(open_tabs=projects, active_tab=projects[0] if projects else None)
        else:
            state = UiState.model_validate(raw)
            state.open_tabs = [t for t in dict.fromkeys(state.open_tabs) if t in projects]
        if state.active_tab not in state.open_tabs:
            state.active_tab = state.open_tabs[0] if state.open_tabs else None
        return state

    async def put_ui_state(self, state: UiState) -> UiState:
        await self.store.set_kv(UI_STATE_KEY, state.model_dump(mode="json"))
        state = await self.get_ui_state()
        self.bus.publish("ui_state.updated", {"ui_state": state.model_dump(mode="json")})
        return state

    async def _remember_selection(self, sel: Selection) -> None:
        state = await self.get_ui_state()
        state.last_selection[sel.project] = LastSelection(
            machine=sel.machine,
            image=sel.image,
            venv=sel.venv,
            config=sel.config,
            cuda_visible_devices=sel.cuda_visible_devices,
            params={**state.last_selection.get(sel.project, LastSelection()).params, **sel.params},
        )
        await self.store.set_kv(UI_STATE_KEY, state.model_dump(mode="json"))
        self.bus.publish("ui_state.updated", {"ui_state": state.model_dump(mode="json")})

    # -- preview and launch --------------------------------------------------------------

    def _spec(self, sel: Selection, run_id: str, series: Series | None = None) -> RunSpec:
        try:
            spec = make_spec(
                self.inventory,
                run_id=run_id,
                project=sel.project,
                machine=sel.machine,
                image=sel.image,
                venv=sel.venv,
                config=sel.config,
                command=self.project_command(sel.project),
                overrides=sel.overrides,
                cuda_visible_devices=sel.cuda_visible_devices,
                params=sel.params,
                series=series,
            )
            cmd.validate_spec(spec)
        except UnknownResourceError as exc:
            raise InvalidRequestError(str(exc)) from exc
        except (SelectionError, ValueError) as exc:
            raise InvalidRequestError(str(exc)) from exc
        return spec

    async def preview(self, sel: Selection, run_id: str = "rxxxxx") -> PreviewResult:
        """Full command for a selection, without running anything."""
        spec = self._spec(sel, run_id, (sel.series.plan() if sel.series else None))
        remote = cmd.docker_run(spec)
        argv = self.executor.full_argv(spec.machine, remote)
        return PreviewResult(
            text=cmd.preview(spec),
            exact=shlex.join(argv),
            argv=argv,
            command=cmd.resolved_command(spec),
            container_name=spec.container_name,
        )

    async def _validate_config(self, sel: Selection) -> None:
        cl = await self.get_configs(sel.project, sel.machine)
        if cl.configs and sel.config not in cl.configs:
            raise InvalidRequestError(
                f"unknown config '{sel.config}' for project '{sel.project}'; "
                f"available configs: {', '.join(cl.configs)}"
            )

    async def _new_run_id(self) -> str:
        for _ in range(20):
            rid = new_run_id()
            if await self.store.get_run(rid) is None:
                return rid
        raise ServiceError("could not generate a unique run identifier")

    async def launch(
        self,
        sel: Selection,
        *,
        source: RunSource = RunSource.UI,
        wait: bool | float = False,
    ) -> Run:
        """Create a run and launch its container (``docker run -d``).

        With ``wait=False`` the run is returned immediately with status ``launching``;
        ``wait=True`` waits for the ``docker run`` to finish; a number waits at most that long.
        """
        run_id = await self._new_run_id()
        series = sel.series.plan() if sel.series else None
        spec = self._spec(sel, run_id, series)
        await self._validate_config(sel)
        remote = cmd.docker_run(spec)
        run = Run(
            id=run_id,
            project_id=spec.project.id,
            machine_id=spec.machine.id,
            image_id=spec.image.id,
            venv_id=spec.venv.id,
            config=spec.config,
            command=cmd.resolved_command(spec),
            full_command=self.executor.full_argv(spec.machine, remote),
            container_name=spec.container_name,
            status=RunStatus.LAUNCHING,
            created_at=utcnow(),
            source=source,
            overrides=list(spec.overrides),
            cuda_visible_devices=spec.cuda_visible_devices,
            params=dict(spec.params),
            series=series,
        )
        lock = self._lock(run_id)
        await lock.acquire()
        await self.store.save_run(run)
        self._publish_run("run.created", run)
        await self._remember_selection(sel)
        task = self._spawn(self._do_launch(run, spec.machine, remote, lock))
        await self._wait(task, wait)
        return await self.get_run(run_id)

    async def _wait(self, task: asyncio.Task[Any], wait: bool | float) -> None:
        if wait is False:
            return
        timeout = None if wait is True else float(wait)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout)

    async def _do_launch(
        self, run: Run, machine: Machine, remote: list[str], lock: asyncio.Lock
    ) -> None:
        try:
            try:
                res = await self.executor.exec(machine, remote, timeout=LAUNCH_TIMEOUT_S)
            except (MachineUnreachableError, ExecutorError) as exc:
                await self._update(
                    run, status=RunStatus.FAILED, error=str(exc), finished_at=utcnow()
                )
                return
            if not res.ok:
                await self._update(
                    run,
                    status=RunStatus.FAILED,
                    error=f"launch failed: {res.error_message()}",
                    finished_at=utcnow(),
                    exit_code=None,
                )
                return
            now = utcnow()
            await self._update(
                run, status=RunStatus.RUNNING, started_at=now, error=None, container_alive=True
            )
            self.logs.follow(run.id)
        finally:
            lock.release()

    # -- reading -------------------------------------------------------------------------

    async def get_run(self, run_id: str) -> Run:
        run = await self.store.get_run(run_id)
        if run is None:
            raise NotFoundError(f"run '{run_id}' not found")
        return run

    async def list_runs(
        self,
        *,
        project: str | None = None,
        status: RunStatus | list[RunStatus] | None = None,
        limit: int | None = None,
        machine: str | None = None,
    ) -> list[Run]:
        statuses: set[RunStatus] | None
        if status is None:
            statuses = None
        elif isinstance(status, list):
            statuses = set(status)
        else:
            statuses = {status}
        return await self.store.list_runs(
            project=project, statuses=statuses, limit=limit, machine=machine
        )

    async def snapshot(self) -> Snapshot:
        now = utcnow()
        runs = await self.store.list_runs()
        return Snapshot(
            seq=self.bus.seq,
            inventory=self.inventory_view(),
            runs=[RunView.of(r, now) for r in runs],
            ui_state=await self.get_ui_state(),
            server_time=now,
            version=self.version,
        )

    def health(self) -> Health:
        machines = [self.machine_state(m.id) for m in self.inventory.machines]
        return Health(
            ok=self.inv_mgr.error is None,
            executor=self.executor.kind,
            machines=machines,
            inventory_error=self.inv_mgr.error,
            version=self.version,
        )

    # -- actions ------------------------------------------------------------------------

    def _lock(self, run_id: str) -> asyncio.Lock:
        lock = self._locks.get(run_id)
        if lock is None:
            lock = self._locks[run_id] = asyncio.Lock()
        return lock

    def is_busy(self, run_id: str) -> bool:
        lock = self._locks.get(run_id)
        return lock is not None and lock.locked()

    def _check_allowed(self, run: Run, action: str) -> None:
        if run.status not in ALLOWED_FROM[action]:
            label = _ACTION_LABELS.get(action, action)
            possible = ", ".join(allowed_actions(run.status, container_alive=run.container_alive))
            raise ActionNotAllowedError(
                f"cannot {label} run {run.id}: status '{run.status}' "
                f"(possible actions: {possible or 'none'})"
            )

    async def _acquire(self, run: Run) -> asyncio.Lock:
        lock = self._lock(run.id)
        if lock.locked():
            raise ActionNotAllowedError(f"an action is already in progress on run {run.id}")
        await lock.acquire()
        return lock

    async def _exec_action(self, run: Run, remote: list[str], timeout: float) -> ExecResult:
        machine = self._machine(run.machine_id)
        try:
            return await self.executor.exec(machine, remote, timeout=timeout)
        except MachineUnreachableError as exc:
            raise UnreachableError(str(exc)) from exc
        except ExecutorError as exc:
            raise ServiceError(str(exc)) from exc

    async def action(
        self,
        run_id: str,
        action: RunAction | str,
        *,
        source: RunSource = RunSource.UI,
        wait: bool | float = False,
    ) -> Run:
        """Apply an action (pause, resume, stop, restart, relaunch)."""
        try:
            act = RunAction(action)
        except ValueError as exc:
            raise InvalidRequestError(
                f"unknown action: {action!r} (possible: {', '.join(a.value for a in RunAction)})"
            ) from exc
        if act is RunAction.PAUSE:
            return await self.pause(run_id)
        if act is RunAction.RESUME:
            return await self.resume(run_id)
        if act is RunAction.STOP:
            return await self.stop(run_id, wait=wait)
        if act is RunAction.RESTART:
            return await self.restart(run_id)
        if act is RunAction.CLOSE:
            return await self.close_container(run_id)
        return await self.relaunch(run_id, source=source, wait=wait)

    async def pause(self, run_id: str) -> Run:
        run = await self.get_run(run_id)
        self._check_allowed(run, RunAction.PAUSE)
        lock = await self._acquire(run)
        try:
            res = await self._exec_action(
                run, cmd.docker_pause(run.container_name), ACTION_TIMEOUT_S
            )
            if not res.ok:
                raise ServiceError(f"pause failed: {res.error_message()}")
            return await self._update(run, status=RunStatus.PAUSED, paused_at=utcnow())
        finally:
            lock.release()

    async def resume(self, run_id: str) -> Run:
        run = await self.get_run(run_id)
        self._check_allowed(run, RunAction.RESUME)
        lock = await self._acquire(run)
        try:
            res = await self._exec_action(
                run, cmd.docker_unpause(run.container_name), ACTION_TIMEOUT_S
            )
            if not res.ok:
                raise ServiceError(f"resume failed: {res.error_message()}")
            return await self._update(run, status=RunStatus.RUNNING, **_close_pause(run))
        finally:
            lock.release()

    async def stop(self, run_id: str, *, wait: bool | float = False) -> Run:
        """Stop the run (``docker stop -t <stop_timeout>``, after ``unpause`` if paused)."""
        run = await self.get_run(run_id)
        self._check_allowed(run, RunAction.STOP)
        lock = await self._acquire(run)
        previous = run.status
        try:
            run = await self._update(run, status=RunStatus.STOPPING, stop_requested=True)
        except BaseException:
            lock.release()
            raise
        task = self._spawn(self._do_stop(run, previous, lock))
        await self._wait(task, wait)
        return await self.get_run(run_id)

    async def _do_stop(self, run: Run, previous: RunStatus, lock: asyncio.Lock) -> None:
        try:
            timeout = self._stop_timeout(run)
            try:
                if previous is RunStatus.PAUSED:
                    res = await self._exec_action(
                        run, cmd.docker_unpause(run.container_name), ACTION_TIMEOUT_S
                    )
                    run = await self._update(run, **_close_pause(run))
                res = await self._exec_action(
                    run, cmd.docker_stop(run.container_name, timeout), timeout + ACTION_TIMEOUT_S
                )
            except ServiceError as exc:
                # The stop did not happen: forget it so that monitoring does not stay stuck
                # in “stopping” when the machine comes back; the user can try again.
                await self._update(
                    run,
                    status=RunStatus.UNREACHABLE if isinstance(exc, UnreachableError) else previous,
                    error=f"stop failed: {exc.message}",
                    stop_requested=False,
                )
                return
            if not res.ok:
                await self._update(
                    run, status=previous, error=f"stop failed: {res.error_message()}",
                    stop_requested=False,
                )  # fmt: skip
                return
            state = await self._inspect(run)
            await self._update(
                run,
                status=RunStatus.STOPPED,
                exit_code=state.exit_code if state else None,
                finished_at=(state.finished_at if state and state.finished_at else utcnow()),
                error=None,
                container_alive=state.alive if state else False,
                **_close_pause(run),
            )
            self.logs.finish(run.id)
        finally:
            lock.release()

    def _stop_timeout(self, run: Run) -> int:
        try:
            return self.inventory.project(run.project_id).docker.stop_timeout
        except UnknownResourceError:
            return 30

    async def restart(self, run_id: str) -> Run:
        """Restart the same container (``docker start``, or ``docker restart`` if it was kept
        started): the command starts over."""
        run = await self.get_run(run_id)
        self._check_allowed(run, RunAction.RESTART)
        lock = await self._acquire(run)
        try:
            if run.container_alive:
                res = await self._exec_action(
                    run, cmd.docker_restart(run.container_name, 10), 10 + ACTION_TIMEOUT_S
                )
            else:
                res = await self._exec_action(
                    run, cmd.docker_start(run.container_name), ACTION_TIMEOUT_S
                )
            if not res.ok:
                raise ServiceError(f"restart failed: {res.error_message()}")
            run = await self._update(
                run,
                status=RunStatus.RUNNING,
                started_at=utcnow(),
                finished_at=None,
                exit_code=None,
                error=None,
                paused_total_s=0.0,
                paused_at=None,
                stop_requested=False,
                container_alive=True,
                mlflow=None,  # the command starts over: a new MLflow run will be created
                **_reset_series(run),
            )
            self.logs.restart(run.id)
            return run
        finally:
            lock.release()

    async def close_container(self, run_id: str) -> Run:
        """Stop the kept container of a finished run (the run status does not change)."""
        run = await self.get_run(run_id)
        self._check_allowed(run, RunAction.CLOSE)
        if not run.container_alive:
            raise ActionNotAllowedError(f"the container of run {run.id} is already stopped")
        lock = await self._acquire(run)
        try:
            res = await self._exec_action(
                run, cmd.docker_stop(run.container_name, 10), 10 + ACTION_TIMEOUT_S
            )
            if not res.ok and "no such container" not in res.err.lower():
                raise ServiceError(f"close failed: {res.error_message()}")
            return await self._update(run, container_alive=False)
        finally:
            lock.release()

    async def open_shell(self, run_id: str) -> ShellSession:
        """Interactive shell in a run's container (venv activated, in the workdir)."""
        run = await self.get_run(run_id)
        if run.status is RunStatus.PAUSED:
            raise ActionNotAllowedError(f"run {run.id} is paused: resume it first")
        if not (run.container_alive or run.status in (RunStatus.RUNNING, RunStatus.STOPPING)):
            raise ActionNotAllowedError(
                f"the container of run {run.id} is not started "
                "(“restart” starts it again, command included)"
            )
        machine = self._machine(run.machine_id)
        try:
            venv = self.inventory.venv(run.venv_id)
        except UnknownResourceError:
            venv = None
        try:
            workdir = self.inventory.project(run.project_id).workdir
        except UnknownResourceError:
            workdir = None
        remote = cmd.docker_exec_shell(
            run.container_name, venv_path=venv.path if venv and venv.path else None, workdir=workdir
        )
        try:
            return await self.executor.open_shell(machine, remote)
        except MachineUnreachableError as exc:
            raise UnreachableError(str(exc)) from exc
        except ExecutorError as exc:
            raise ServiceError(str(exc)) from exc

    async def relaunch(
        self, run_id: str, *, source: RunSource = RunSource.UI, wait: bool | float = False
    ) -> Run:
        """New run with the same parameters (current project command)."""
        run = await self.get_run(run_id)
        self._check_allowed(run, RunAction.RELAUNCH)
        current = command_params(self.project_command(run.project_id))
        series = None
        if run.series is not None:
            # same repetitions, same values (those still in the current command)
            series = SeriesRequest(
                count=run.series.count,
                continue_on_failure=run.series.continue_on_failure,
                values=[
                    {k: v for k, v in values.items() if k in current}
                    for values in run.series.values
                ],
            )
        sel = Selection(
            project=run.project_id,
            machine=run.machine_id,
            image=run.image_id,
            venv=run.venv_id,
            config=run.config,
            overrides=run.overrides,
            cuda_visible_devices=run.cuda_visible_devices,
            # parameters still present in the current project command
            params={k: v for k, v in run.params.items() if k in current},
            series=series,
        )
        return await self.launch(sel, source=source, wait=wait)

    async def delete(self, run_id: str) -> None:
        """Delete the container (``docker rm``) then the run."""
        run = await self.get_run(run_id)
        self._check_allowed(run, "delete")
        lock = await self._acquire(run)
        try:
            try:
                machine = self.inventory.machine(run.machine_id)
            except UnknownResourceError:
                machine = None
            if machine is not None:
                res = await self._exec_action(
                    run,
                    cmd.docker_rm(run.container_name, force=run.container_alive),
                    ACTION_TIMEOUT_S + 10,
                )
                if not res.ok and "no such container" not in res.err.lower():
                    raise ServiceError(f"delete failed: {res.error_message()}")
            await self.store.delete_run(run.id)
            self.logs.discard(run.id)
            self._series_trackers.pop(run.id, None)
            self.bus.publish("run.deleted", {"id": run.id, "project_id": run.project_id})
        finally:
            lock.release()
            self._locks.pop(run_id, None)

    # -- logs ---------------------------------------------------------------------------

    async def _open_log_stream(self, run_id: str) -> LogStream:
        """Full ``docker logs`` (followed if the run is active); raises :class:`LogsGoneError`
        if the container no longer exists."""
        run = await self.get_run(run_id)
        machine = self._machine(run.machine_id)
        res = await self.executor.exec(machine, cmd.docker_inspect(run.container_name))
        if not res.ok and "no such" in (res.err + res.out).lower():
            raise LogsGoneError(run.container_name)
        return await self.executor.stream(
            machine,
            cmd.docker_logs(run.container_name, follow=run.status.is_active, tail=None),
        )

    async def _is_finished(self, run_id: str) -> bool:
        """Is the run finished? Checks the actual container state if the database says “active”."""
        run = await self.store.get_run(run_id)
        if run is None or run.status in FINISHED_STATUSES:
            return True
        if self.is_busy(run_id):
            return False
        state = await self._inspect(run)
        if state is None:
            return False
        await self._apply_state(run, state)
        fresh = await self.store.get_run(run_id)
        return fresh is None or fresh.status in FINISHED_STATUSES

    async def subscribe_logs(self, run_id: str) -> LogSubscriber:
        await self.get_run(run_id)
        return await self.logs.subscribe(run_id)

    def unsubscribe_logs(self, sub: LogSubscriber) -> None:
        self.logs.unsubscribe(sub)

    async def stream_logs(self, run_id: str, *, until_end: bool = True) -> AsyncIterator[bytes]:
        """Raw logs of a run: buffer then live stream (stops when the run ends)."""
        await self.get_run(run_id)
        async for chunk in self.logs.stream(run_id, until_end=until_end):
            yield chunk

    def log_path(self, run_id: str) -> Path | None:
        """Log backup file of a run (may not exist)."""
        return self.logs.log_path(run_id)

    async def read_logs(self, run_id: str, tail: int | None = None) -> bytes:
        """Raw logs: backup file, else ``docker logs --tail``, else the buffer."""
        run = await self.get_run(run_id)
        saved = self.logs.read_file(run_id)
        if saved:
            return _tail_lines(saved, tail)
        try:
            res = await self._exec_action(
                run, cmd.docker_logs(run.container_name, follow=False, tail=tail), 30.0
            )
        except UnreachableError:
            buf = self.logs.buffer(run_id)
            if buf is not None:
                return buf
            raise
        if not res.ok:
            buf = self.logs.buffer(run_id)
            if buf is not None:
                return buf
            raise ServiceError(f"logs unavailable: {res.error_message()}")
        # Without a TTY, docker logs separates stdout and stderr: concatenate them.
        return res.stdout + res.stderr

    async def read_logs_since(self, run_id: str, since: int = 0) -> LogSlice:
        """Raw logs from byte offset ``since`` (the ``cursor`` of an earlier read).

        Offsets count bytes of the full log (backup file, else ``docker logs``). If the logs
        are now shorter than ``since`` (rewritten), they are returned from the start with
        ``reset``.
        """
        await self.get_run(run_id)
        got = self.logs.read_file_from(run_id, since)
        if got is not None:
            data, size = got
            reset = since > size
        else:
            full = await self.read_logs(run_id)
            reset = since > len(full)
            data = full if reset else full[since:]
        start = 0 if reset else since
        return LogSlice(data=data, start=start, cursor=start + _complete_utf8(data), reset=reset)

    async def wait_run(
        self,
        run_id: str,
        *,
        timeout: float,
        pattern: str | None = None,
        since: int | None = None,
        until: WaitUntil = "finished",
        on_tick: Callable[[float, Run], Awaitable[None]] | None = None,
    ) -> WaitResult:
        """Wait until the run finishes, a log line matches ``pattern`` or ``timeout`` expires.

        ``pattern`` (case-insensitive regex) is searched in the cleaned lines written after
        ``since`` (default: the end of the logs at call time). ``until="status_change"`` also
        returns as soon as the status differs from the initial one. ``on_tick`` is called on
        every polling round (progress).
        """
        rx: re.Pattern[str] | None = None
        if pattern:
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise InvalidRequestError(f"invalid regex {pattern!r}: {exc}") from exc
        t0 = time.monotonic()
        run = await self.get_run(run_id)
        initial = run.status
        cursor: int | None = None
        carry = b""  # unfinished last line, searched again with what follows
        if rx is not None:
            cursor = since if since is not None else (await self.read_logs_since(run_id)).cursor

        async def scan() -> str | None:
            nonlocal cursor, carry
            assert rx is not None and cursor is not None
            sl = await self.read_logs_since(run_id, cursor)
            if sl.reset:
                carry = b""
            chunk = carry + sl.data[: sl.cursor - sl.start]
            cursor = sl.cursor
            nl = chunk.rfind(b"\n")
            carry = chunk[nl + 1 :][-_CARRY_MAX:]
            for line in clean_logs(chunk).split("\n"):
                if rx.search(line):
                    return line.strip()[:500]
            return None

        def result(reason: WaitReason, run: Run, match: str | None = None) -> WaitResult:
            return WaitResult(run, reason, time.monotonic() - t0, match, cursor)

        poll = min(WAIT_POLL_S, self.poll_interval)
        while True:
            if run.status.is_finished:
                # the last lines may still be arriving: give them a chance to match
                match = await scan() if rx is not None else None
                return result("finished", run, match)
            if rx is not None and (match := await scan()) is not None:
                return result("match", run, match)
            if until == "status_change" and run.status is not initial:
                return result("status_change", run)
            elapsed = time.monotonic() - t0
            if elapsed >= timeout:
                return result("timeout", run)
            if on_tick is not None:
                await on_tick(elapsed, run)
            await asyncio.sleep(min(poll, max(0.0, timeout - elapsed)))
            run = await self.get_run(run_id)

    # -- GPUs ---------------------------------------------------------------------------

    async def _active_runs_on(self, machine_id: str) -> list[Run]:
        return [r for r in await self.store.list_runs(machine=machine_id) if r.status.is_active]

    @staticmethod
    def _gpu_runs(runs: list[Run]) -> list[GpuRunView]:
        return [
            GpuRunView(
                id=r.id,
                project=r.project_id,
                status=r.status,
                cuda_visible_devices=r.cuda_visible_devices,
            )
            for r in runs
        ]

    async def _query_gpus(
        self, machine: Machine, runs: list[Run]
    ) -> tuple[list[GpuView], str | None]:
        """GPUs of ``machine`` (``nvidia-smi``) and an error message if they are unknown.

        Raises :class:`MachineUnreachableError` / :class:`ExecutorError` from the executor.
        """
        gpus_res, apps_res = await asyncio.gather(
            self.executor.exec(machine, gpus.query_gpus()),
            self.executor.exec(machine, gpus.query_apps()),
        )
        if not gpus_res.ok:
            return [], f"nvidia-smi failed: {gpus_res.error_message()}"
        pinned: dict[int, list[str]] = {}
        for r in runs:
            for i in gpus.visible_indices(r.cuda_visible_devices) or []:
                pinned.setdefault(i, []).append(r.id)
        return [
            GpuView(
                index=g.index,
                name=g.name,
                memory_used_mib=g.memory_used_mib,
                memory_total_mib=g.memory_total_mib,
                utilization_pct=g.utilization_pct,
                temperature_c=g.temperature_c,
                processes=g.processes,
                runs=pinned.get(g.index, []),
            )
            for g in gpus.parse_gpus(gpus_res.out, apps_res.out if apps_res.ok else "")
        ], None

    async def machine_gpus(self, machine_id: str) -> MachineGpus:
        """GPUs of a machine (``nvidia-smi`` on the host) and the active runs placed on it."""
        machine = self._machine(machine_id)
        runs = await self._active_runs_on(machine_id)
        report = MachineGpus(
            machine=machine_id, active_runs=self._gpu_runs(runs), checked_at=utcnow()
        )
        try:
            report.gpus, report.error = await self._query_gpus(machine, runs)
        except MachineUnreachableError as exc:
            raise UnreachableError(str(exc)) from exc
        except ExecutorError as exc:
            raise ServiceError(str(exc)) from exc
        return report

    # -- machine load ---------------------------------------------------------------------

    async def _query_cpu(self, machine: Machine) -> tuple[CpuView | None, str | None]:
        res = await self.executor.exec(machine, load.query_cpu())
        if not res.ok:
            return None, f"CPU load unavailable: {res.error_message()}"
        try:
            c = load.parse_cpu(res.out)
        except ValueError as exc:
            return None, f"CPU load unavailable: {exc}"
        return CpuView(
            cores=c.cores,
            usage_pct=c.usage_pct,
            per_core_pct=c.per_core_pct,
            load_avg=c.load_avg,
            memory_used_mib=c.memory_used_mib,
            memory_total_mib=c.memory_total_mib,
        ), None

    async def machine_load(self, machine_id: str) -> MachineLoad:
        """CPU (``/proc``) and GPU (``nvidia-smi``) load of a machine, never raises for an
        unreachable machine (``reachable: false`` and ``error``)."""
        machine = self._machine(machine_id)
        runs = await self._active_runs_on(machine_id)
        report = MachineLoad(
            machine=machine_id,
            reachable=True,
            active_runs=self._gpu_runs(runs),
            checked_at=utcnow(),
        )
        try:
            (report.cpu, report.cpu_error), (report.gpus, report.gpu_error) = await asyncio.gather(
                self._query_cpu(machine), self._query_gpus(machine, runs)
            )
        except ExecutorError as exc:
            report.reachable = False
            report.error = exc.reason if isinstance(exc, MachineUnreachableError) else str(exc)
        return report

    # -- MLflow --------------------------------------------------------------------------

    async def set_mlflow(
        self, run_id: str, info: MlflowInfo, *, started_at: datetime | None = None
    ) -> None:
        """Record the MLflow state of a run (published as ``run.updated``).

        Ignored if the run was restarted since ``started_at`` (state of the previous run).
        """
        run = await self.store.get_run(run_id)
        if run is not None and run.started_at == started_at:
            await self._update(run, mlflow=info)

    # -- series ----------------------------------------------------------------------------

    def _observe_logs(self, run_id: str, data: bytes | None) -> None:
        """Follow the progress of series from the markers of their logs."""
        if data is None:  # the history starts over: it will be read again
            self._series_trackers[run_id] = SeriesTracker()
            return
        tracker = self._series_trackers.setdefault(run_id, SeriesTracker())
        if tracker.feed(data):
            self._spawn(
                self._apply_series_progress(run_id, tracker.current, list(tracker.exit_codes))
            )

    async def _apply_series_progress(self, run_id: str, current: int, codes: list[int]) -> None:
        async with self._series_lock:
            run = await self.store.get_run(run_id)
            if run is None or run.series is None:
                return
            s = run.series
            # Only move forward: logs read again from the start replay the past states.
            if current + len(codes) <= s.current + len(s.exit_codes):
                return
            codes = codes[: s.count]
            await self._update(
                run,
                series=s.model_copy(update={"current": min(current, s.count), "exit_codes": codes}),
            )

    # -- updates and monitoring ----------------------------------------------------------

    async def _inspect(self, run: Run) -> ContainerState | None:
        try:
            res = await self._exec_action(
                run, cmd.docker_inspect(run.container_name), ACTION_TIMEOUT_S
            )
        except ServiceError:
            return None
        return parse_inspect(res.out) if res.ok else None

    def _publish_run(self, type_: str, run: Run) -> None:
        self.bus.publish(type_, {"run": RunView.of(run).model_dump(mode="json")})

    async def _update(self, run: Run, **changes: Any) -> Run:
        """Apply changes, persist and publish ``run.updated`` if needed."""
        fresh = await self.store.get_run(run.id)
        if fresh is None:
            return run
        updated = fresh.model_copy(update=changes)
        if updated != fresh:
            await self.store.save_run(updated)
            self._publish_run("run.updated", updated)
            if updated.status != fresh.status:
                log.info(
                    "run %s: %s → %s",
                    run.id,
                    fresh.status,
                    updated.status,
                    extra={"run_id": run.id, "from": str(fresh.status), "to": str(updated.status)},
                )
        return updated

    def set_machine_status(
        self,
        machine_id: str,
        *,
        reachable: bool,
        error: str | None = None,
        latency_ms: float | None = None,
    ) -> MachineStatus:
        prev = self.machine_state(machine_id)
        now = utcnow()
        st = MachineStatus(
            id=machine_id,
            reachable=reachable,
            last_check=now,
            last_ok=now if reachable else prev.last_ok,
            error=error,
            latency_ms=latency_ms,
        )
        self.machine_status[machine_id] = st
        if prev.reachable != reachable or prev.error != error:
            self.bus.publish("machine.status", {"machine": st.model_dump(mode="json")})
        return st

    async def mark_machine_unreachable(self, machine_id: str, reason: str) -> None:
        """Switch the machine's active runs to ``unreachable``."""
        self.set_machine_status(machine_id, reachable=False, error=reason)
        for run in await self.store.list_runs(machine=machine_id):
            if run.status in FINISHED_STATUSES or run.status is RunStatus.LAUNCHING:
                continue
            if self.is_busy(run.id):
                continue
            await self._update(
                run, status=RunStatus.UNREACHABLE, error=f"machine unreachable: {reason}"
            )

    async def apply_container_states(
        self, machine_id: str, containers: list[ContainerState], *, reconcile: bool = False
    ) -> None:
        """Reconcile the runs in the database with the containers seen on the machine."""
        by_name = {c.name: c for c in containers}
        runs = await self.store.list_runs(machine=machine_id)
        known = {r.container_name for r in runs}
        if reconcile:
            for c in containers:
                if c.name not in known and c.run_id:
                    await self._import_container(machine_id, c)
        for run in runs:
            if self.is_busy(run.id):
                continue
            found = by_name.get(run.container_name)
            if found is None:
                if run.container_alive:
                    await self._update(run, container_alive=False)
                    run = await self.get_run(run.id)
                if run.status.is_active:
                    await self._update(
                        run,
                        status=RunStatus.FAILED,
                        error="container not found on the machine (deleted outside TrainHub?)",
                        finished_at=run.finished_at or utcnow(),
                        **_close_pause(run),
                    )
                continue
            await self._apply_state(run, found)

    async def _apply_state(self, run: Run, c: ContainerState) -> None:
        new = status_from_container(c, stop_requested=run.stop_requested)
        changes: dict[str, Any] = {}
        if run.container_alive != c.alive:
            changes["container_alive"] = c.alive
        if run.status is RunStatus.UNREACHABLE:
            changes["error"] = None
        if new == run.status and not changes:
            if run.status.is_active:
                self.logs.follow(run.id)
            return
        if new.is_finished:
            if run.status.is_finished:
                # already finished (avoid overwriting a stopped status): only the container
                # state may change (kept container closed outside TrainHub…).
                if "container_alive" in changes:
                    await self._update(run, container_alive=c.alive)
                return
            st = await self._inspect(run)
            if st is not None:
                # docker ps does not give the code of a kept container: inspect does.
                new = status_from_container(st, stop_requested=run.stop_requested)
                changes["container_alive"] = st.alive
            elif c.command_done:
                return  # unknown exit code: wait for the next round
            if new.is_finished:
                changes.update(
                    status=new,
                    exit_code=st.exit_code if st and st.exit_code is not None else c.exit_code,
                    finished_at=(
                        st.finished_at if st and st.finished_at and not st.alive else utcnow()
                    ),
                    **_close_pause(run),
                )
                if new is RunStatus.STOPPED:
                    changes["error"] = None
                await self._update(run, **changes)
                self.logs.finish(run.id)
                return
        changes["status"] = new
        if new is RunStatus.PAUSED and run.paused_at is None:
            changes["paused_at"] = utcnow()
        elif new is RunStatus.RUNNING:
            changes.update(_close_pause(run))
        if run.status.is_finished:
            # Restarted outside TrainHub.
            st = await self._inspect(run)
            changes.update(
                started_at=st.started_at if st and st.started_at else utcnow(),
                finished_at=None,
                exit_code=None,
                stop_requested=False,
                **_reset_series(run),
            )
            self.logs.restart(run.id)
        updated = await self._update(run, **changes)
        if updated.status.is_active:
            self.logs.follow(run.id)

    async def _import_container(self, machine_id: str, c: ContainerState) -> None:
        run_id = c.run_id
        if not run_id:
            return
        existing = await self.store.get_run(run_id)
        if existing is not None:
            return
        machine = self._machine(machine_id)
        st: ContainerState | None = None
        try:
            res = await self.executor.exec(machine, cmd.docker_inspect(c.name))
            st = parse_inspect(res.out) if res.ok else None
        except ExecutorError:
            st = None
        labels = (st.labels if st else None) or c.labels
        status = status_from_container(st or c, stop_requested=False)
        run = Run(
            id=run_id,
            project_id=labels.get(cmd.LABEL_PROJECT, "?"),
            machine_id=machine_id,
            image_id=labels.get(cmd.LABEL_IMAGE, c.image),
            venv_id=labels.get(cmd.LABEL_VENV, "?"),
            config=labels.get(cmd.LABEL_CONFIG, "?"),
            command="(imported from an existing container)",
            full_command=[],
            container_name=c.name,
            status=status,
            exit_code=(st.exit_code if st else c.exit_code) if status.is_finished else None,
            created_at=(st.created_at if st and st.created_at else c.created_at) or utcnow(),
            started_at=st.started_at if st else None,
            finished_at=(st.finished_at if st else None) if status.is_finished else None,
            paused_at=utcnow() if status is RunStatus.PAUSED else None,
            source=RunSource.IMPORT,
            container_alive=(st or c).alive,
        )
        await self.store.save_run(run)
        self._publish_run("run.created", run)
        log.info("container imported", extra={"run_id": run_id, "machine": machine_id})


def _tail_lines(data: bytes, tail: int | None) -> bytes:
    """The last ``tail`` lines of ``data`` (everything if ``None``)."""
    if tail is None:
        return data
    if tail <= 0:
        return b""
    idx = len(data) - 1 if data.endswith(b"\n") else len(data)
    for _ in range(tail):
        idx = data.rfind(b"\n", 0, idx)
        if idx < 0:
            return data
    return data[idx + 1 :]


def _complete_utf8(data: bytes) -> int:
    """Length of ``data`` without a trailing incomplete UTF-8 sequence."""
    for back in range(1, min(4, len(data)) + 1):
        b = data[-back]
        if b < 0x80:
            return len(data)
        if b >= 0xC0:  # lead byte: complete if its sequence fits in what follows
            need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            return len(data) if back >= need else len(data) - back
    return len(data)


def _reset_series(run: Run) -> dict[str, Any]:
    """Fields to update when the command of a series starts over."""
    if run.series is None:
        return {}
    return {"series": run.series.model_copy(update={"current": 0, "exit_codes": []})}


def _close_pause(run: Run) -> dict[str, Any]:
    """Fields to update to close an ongoing pause."""
    if run.paused_at is None:
        return {}
    extra = max(0.0, (utcnow() - run.paused_at).total_seconds())
    return {"paused_total_s": run.paused_total_s + extra, "paused_at": None}
