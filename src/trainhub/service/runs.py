"""Couche service : cycle de vie des runs, persistance, réconciliation, bus d'événements.

C'est le seul point d'entrée de la logique métier : l'API REST, le WebSocket, le serveur MCP,
la classe :class:`trainhub.TrainHub` et la CLI l'appellent tous.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import shlex
import time
from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..core import commands as cmd
from ..core.commands import RunSpec
from ..core.ids import new_run_id
from ..core.models import (
    FINISHED_STATUSES,
    Inventory,
    Machine,
    Project,
    Run,
    RunSource,
    RunStatus,
    UnknownResourceError,
    validate_command_template,
)
from ..core.selection import SelectionError, make_spec
from ..core.status import ContainerState, parse_inspect, status_from_container, utcnow
from ..executors.base import (
    ACTION_TIMEOUT_S,
    LAUNCH_TIMEOUT_S,
    ExecResult,
    Executor,
    ExecutorError,
    LogStream,
    MachineUnreachableError,
)
from .events import EventBus
from .inventory_watch import InventoryManager
from .log_hub import LogHub, LogSubscriber
from .schemas import (
    ConfigList,
    Health,
    ImageView,
    InventoryView,
    LastSelection,
    MachineStatus,
    MachineView,
    PreviewResult,
    ProjectView,
    RunView,
    Selection,
    Snapshot,
    UiState,
    VenvView,
)
from .store import Store

log = logging.getLogger("trainhub.service")

CONFIG_CACHE_TTL_S = 60.0
UI_STATE_KEY = "ui_state"


# --------------------------------------------------------------------------- erreurs


class ServiceError(Exception):
    """Erreur métier, affichée telle quelle à l'utilisateur."""

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


#: Statuts de départ autorisés pour chaque action (section 4.2).
ALLOWED_FROM: dict[str, frozenset[RunStatus]] = {
    RunAction.PAUSE: frozenset({RunStatus.RUNNING}),
    RunAction.RESUME: frozenset({RunStatus.PAUSED}),
    RunAction.STOP: frozenset({RunStatus.RUNNING, RunStatus.PAUSED}),
    RunAction.RESTART: FINISHED_STATUSES,
    RunAction.RELAUNCH: FINISHED_STATUSES,
    "delete": FINISHED_STATUSES,
}

_ACTION_LABELS = {
    RunAction.PAUSE: "mettre en pause",
    RunAction.RESUME: "reprendre",
    RunAction.STOP: "arrêter",
    RunAction.RESTART: "redémarrer",
    RunAction.RELAUNCH: "relancer",
    "delete": "supprimer",
}


def allowed_actions(status: RunStatus) -> list[str]:
    """Actions possibles pour un statut donné."""
    return [str(a) for a, statuses in ALLOWED_FROM.items() if status in statuses]


# --------------------------------------------------------------------------- service


class RunService:
    """Façade métier de TrainHub."""

    def __init__(
        self,
        inventory: InventoryManager,
        executor: Executor,
        store: Store,
        *,
        bus: EventBus | None = None,
        poll_interval: float = 3.0,
        log_idle_timeout: float = 30.0,
        version: str = "0.1.0",
    ) -> None:
        self.inv_mgr = inventory
        self.executor = executor
        self.store = store
        self.bus = bus or EventBus()
        self.poll_interval = poll_interval
        self.version = version
        self.logs = LogHub(self._open_log_stream, self._is_finished, idle_timeout=log_idle_timeout)
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
        self.executor.update_inventory(self.inventory)
        self.inv_mgr.on_change(self._on_inventory_change)

    # -- cycle de vie -----------------------------------------------------------------

    @property
    def inventory(self) -> Inventory:
        return self.inv_mgr.inventory

    async def start(self, *, monitor: bool = True, watch_inventory: bool = True) -> None:
        """Ouvre la base, démarre le suivi des machines et la surveillance de l'inventaire."""
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

    async def close(self) -> None:
        await self.monitor.stop()
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

    # -- inventaire et projets ----------------------------------------------------------

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
        """Commande générique effective (surcharge persistée, sinon YAML)."""
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
        """Surcharge la commande du projet (``None`` : revenir à la valeur du YAML)."""
        p = self._project(project_id)
        if command is not None:
            command = command.strip()
            try:
                validate_command_template(command)
            except ValueError as exc:
                raise InvalidRequestError(f"commande invalide : {exc}") from exc
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
        """Configs disponibles : liste statique et/ou découverte (cache 60 s)."""
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
                    found, err = await self._discover(machine, d.path, d.pattern)
                    hit = (time.monotonic(), utcnow(), found, err)
                    self._config_cache[key] = hit
                _, cached_at, discovered, error = hit
        configs = list(dict.fromkeys(static + (discovered or [])))
        return ConfigList(
            project=project_id,
            machine=machine_id,
            configs=configs,
            static=static,
            discovered=discovered,
            discover_error=error,
            cached_at=cached_at,
        )

    async def _discover(
        self, machine: Machine, path: str, pattern: str
    ) -> tuple[list[str] | None, str | None]:
        from ..core.models import ConfigDiscover

        try:
            res = await self.executor.exec(
                machine, cmd.list_configs(ConfigDiscover(path=path, pattern=pattern))
            )
        except MachineUnreachableError as exc:
            return None, str(exc)
        except ExecutorError as exc:
            return None, str(exc)
        if not res.ok:
            return None, f"découverte des configs impossible : {res.error_message()}"
        names = []
        suffix = pattern.rsplit("*", 1)[-1] if "*" in pattern else ""
        for line in res.out.splitlines():
            name = line.strip()
            if name and fnmatch.fnmatch(name, pattern):
                names.append(name[: -len(suffix)] if suffix and name.endswith(suffix) else name)
        return sorted(names), None

    # -- état d'interface ------------------------------------------------------------

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
        )
        await self.store.set_kv(UI_STATE_KEY, state.model_dump(mode="json"))
        self.bus.publish("ui_state.updated", {"ui_state": state.model_dump(mode="json")})

    # -- aperçu et lancement -------------------------------------------------------------

    def _spec(self, sel: Selection, run_id: str) -> RunSpec:
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
            )
            cmd.validate_spec(spec)
        except UnknownResourceError as exc:
            raise InvalidRequestError(str(exc)) from exc
        except (SelectionError, ValueError) as exc:
            raise InvalidRequestError(str(exc)) from exc
        return spec

    async def preview(self, sel: Selection, run_id: str = "rxxxxx") -> PreviewResult:
        """Commande complète pour une sélection, sans rien exécuter."""
        spec = self._spec(sel, run_id)
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
                f"config '{sel.config}' inconnue pour le projet '{sel.project}' ; "
                f"configs disponibles : {', '.join(cl.configs)}"
            )

    async def _new_run_id(self) -> str:
        for _ in range(20):
            rid = new_run_id()
            if await self.store.get_run(rid) is None:
                return rid
        raise ServiceError("impossible de générer un identifiant de run unique")

    async def launch(
        self,
        sel: Selection,
        *,
        source: RunSource = RunSource.UI,
        wait: bool | float = False,
    ) -> Run:
        """Crée un run et lance son conteneur (``docker run -d``).

        Avec ``wait=False`` le run est renvoyé immédiatement en statut ``launching`` ;
        ``wait=True`` attend la fin du ``docker run`` ; un nombre attend au plus ce délai.
        """
        run_id = await self._new_run_id()
        spec = self._spec(sel, run_id)
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
                    error=f"échec du lancement : {res.error_message()}",
                    finished_at=utcnow(),
                    exit_code=None,
                )
                return
            now = utcnow()
            await self._update(run, status=RunStatus.RUNNING, started_at=now, error=None)
        finally:
            lock.release()

    # -- lecture ------------------------------------------------------------------------

    async def get_run(self, run_id: str) -> Run:
        run = await self.store.get_run(run_id)
        if run is None:
            raise NotFoundError(f"run '{run_id}' introuvable")
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
            possible = ", ".join(allowed_actions(run.status)) or "aucune"
            raise ActionNotAllowedError(
                f"impossible de {label} le run {run.id} : statut '{run.status}' "
                f"(actions possibles : {possible})"
            )

    async def _acquire(self, run: Run) -> asyncio.Lock:
        lock = self._lock(run.id)
        if lock.locked():
            raise ActionNotAllowedError(f"une action est déjà en cours sur le run {run.id}")
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
        """Applique une action (pause, resume, stop, restart, relaunch)."""
        try:
            act = RunAction(action)
        except ValueError as exc:
            raise InvalidRequestError(
                f"action inconnue : {action!r} "
                f"(possibles : {', '.join(a.value for a in RunAction)})"
            ) from exc
        if act is RunAction.PAUSE:
            return await self.pause(run_id)
        if act is RunAction.RESUME:
            return await self.resume(run_id)
        if act is RunAction.STOP:
            return await self.stop(run_id, wait=wait)
        if act is RunAction.RESTART:
            return await self.restart(run_id)
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
                raise ServiceError(f"pause impossible : {res.error_message()}")
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
                raise ServiceError(f"reprise impossible : {res.error_message()}")
            return await self._update(run, status=RunStatus.RUNNING, **_close_pause(run))
        finally:
            lock.release()

    async def stop(self, run_id: str, *, wait: bool | float = False) -> Run:
        """Arrête le run (``docker stop -t <stop_timeout>``, après ``unpause`` si en pause)."""
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
                # L'arrêt n'a pas eu lieu : on l'oublie pour que le suivi ne reste pas bloqué
                # en « stopping » au retour de la machine ; l'utilisateur pourra réessayer.
                await self._update(
                    run,
                    status=RunStatus.UNREACHABLE if isinstance(exc, UnreachableError) else previous,
                    error=f"arrêt impossible : {exc.message}",
                    stop_requested=False,
                )
                return
            if not res.ok:
                await self._update(
                    run, status=previous, error=f"arrêt impossible : {res.error_message()}",
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
                **_close_pause(run),
            )
        finally:
            lock.release()

    def _stop_timeout(self, run: Run) -> int:
        try:
            return self.inventory.project(run.project_id).docker.stop_timeout
        except UnknownResourceError:
            return 30

    async def restart(self, run_id: str) -> Run:
        """Relance le même conteneur (``docker start``) : la commande repart du début."""
        run = await self.get_run(run_id)
        self._check_allowed(run, RunAction.RESTART)
        lock = await self._acquire(run)
        try:
            res = await self._exec_action(
                run, cmd.docker_start(run.container_name), ACTION_TIMEOUT_S
            )
            if not res.ok:
                raise ServiceError(f"redémarrage impossible : {res.error_message()}")
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
            )
            self.logs.restart(run.id)
            return run
        finally:
            lock.release()

    async def relaunch(
        self, run_id: str, *, source: RunSource = RunSource.UI, wait: bool | float = False
    ) -> Run:
        """Nouveau run avec les mêmes paramètres (commande actuelle du projet)."""
        run = await self.get_run(run_id)
        self._check_allowed(run, RunAction.RELAUNCH)
        sel = Selection(
            project=run.project_id,
            machine=run.machine_id,
            image=run.image_id,
            venv=run.venv_id,
            config=run.config,
            overrides=run.overrides,
            cuda_visible_devices=run.cuda_visible_devices,
        )
        return await self.launch(sel, source=source, wait=wait)

    async def delete(self, run_id: str) -> None:
        """Supprime le conteneur (``docker rm``) puis le run."""
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
                    run, cmd.docker_rm(run.container_name), ACTION_TIMEOUT_S
                )
                if not res.ok and "no such container" not in res.err.lower():
                    raise ServiceError(f"suppression impossible : {res.error_message()}")
            await self.store.delete_run(run.id)
            self.logs.discard(run.id)
            self.bus.publish("run.deleted", {"id": run.id, "project_id": run.project_id})
        finally:
            lock.release()
            self._locks.pop(run_id, None)

    # -- logs ---------------------------------------------------------------------------

    async def _open_log_stream(self, run_id: str) -> LogStream:
        run = await self.get_run(run_id)
        machine = self._machine(run.machine_id)
        return await self.executor.stream(
            machine, cmd.docker_logs(run.container_name, follow=True, tail=2000)
        )

    async def _is_finished(self, run_id: str) -> bool:
        """Le run est-il terminé ? Vérifie l'état réel du conteneur si la base dit « actif »."""
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
        """Logs bruts d'un run : tampon puis flux direct (s'arrête à la fin du run)."""
        await self.get_run(run_id)
        async for chunk in self.logs.stream(run_id, until_end=until_end):
            yield chunk

    async def read_logs(self, run_id: str, tail: int | None = None) -> bytes:
        """Logs bruts (non suivis) : ``docker logs --tail``, ou le tampon si injoignable."""
        run = await self.get_run(run_id)
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
            raise ServiceError(f"logs indisponibles : {res.error_message()}")
        # Sans TTY, docker logs sépare stdout et stderr : on les concatène.
        return res.stdout + res.stderr

    # -- mises à jour et suivi -----------------------------------------------------------

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
        """Applique des changements, persiste et publie ``run.updated`` si besoin."""
        fresh = await self.store.get_run(run.id)
        if fresh is None:
            return run
        updated = fresh.model_copy(update=changes)
        if updated != fresh:
            await self.store.save_run(updated)
            self._publish_run("run.updated", updated)
            if updated.status != fresh.status:
                log.info(
                    "run %s : %s → %s",
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
        """Passe les runs actifs de la machine en ``unreachable``."""
        self.set_machine_status(machine_id, reachable=False, error=reason)
        for run in await self.store.list_runs(machine=machine_id):
            if run.status in FINISHED_STATUSES or run.status is RunStatus.LAUNCHING:
                continue
            if self.is_busy(run.id):
                continue
            await self._update(
                run, status=RunStatus.UNREACHABLE, error=f"machine injoignable : {reason}"
            )

    async def apply_container_states(
        self, machine_id: str, containers: list[ContainerState], *, reconcile: bool = False
    ) -> None:
        """Rapproche les runs en base des conteneurs vus sur la machine."""
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
                if run.status.is_active:
                    await self._update(
                        run,
                        status=RunStatus.FAILED,
                        error="conteneur introuvable sur la machine "
                        "(supprimé en dehors de TrainHub ?)",
                        finished_at=run.finished_at or utcnow(),
                        **_close_pause(run),
                    )
                continue
            await self._apply_state(run, found)

    async def _apply_state(self, run: Run, c: ContainerState) -> None:
        new = status_from_container(c, stop_requested=run.stop_requested)
        changes: dict[str, Any] = {}
        if run.status is RunStatus.UNREACHABLE:
            changes["error"] = None
        if new == run.status and not changes:
            return
        if new.is_finished:
            if run.status.is_finished:
                return  # déjà terminé : rien à faire (évite d'écraser un statut stopped)
            st = await self._inspect(run)
            changes.update(
                status=new,
                exit_code=st.exit_code if st and st.exit_code is not None else c.exit_code,
                finished_at=(st.finished_at if st and st.finished_at else utcnow()),
                **_close_pause(run),
            )
            if new is RunStatus.STOPPED:
                changes["error"] = None
        else:
            changes["status"] = new
            if new is RunStatus.PAUSED and run.paused_at is None:
                changes["paused_at"] = utcnow()
            elif new is RunStatus.RUNNING:
                changes.update(_close_pause(run))
            if run.status.is_finished:
                # Redémarré en dehors de TrainHub.
                st = await self._inspect(run)
                changes.update(
                    started_at=st.started_at if st and st.started_at else utcnow(),
                    finished_at=None,
                    exit_code=None,
                    stop_requested=False,
                )
                self.logs.restart(run.id)
        updated = await self._update(run, **changes)
        if updated.status.is_active:
            self.logs.ensure_following(run.id)

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
            command="(importé depuis un conteneur existant)",
            full_command=[],
            container_name=c.name,
            status=status,
            exit_code=(st.exit_code if st else c.exit_code) if status.is_finished else None,
            created_at=(st.created_at if st and st.created_at else c.created_at) or utcnow(),
            started_at=st.started_at if st else None,
            finished_at=(st.finished_at if st else None) if status.is_finished else None,
            paused_at=utcnow() if status is RunStatus.PAUSED else None,
            source=RunSource.IMPORT,
        )
        await self.store.save_run(run)
        self._publish_run("run.created", run)
        log.info("conteneur importé", extra={"run_id": run_id, "machine": machine_id})


def _close_pause(run: Run) -> dict[str, Any]:
    """Champs à mettre à jour pour clore une pause en cours."""
    if run.paused_at is None:
        return {}
    extra = max(0.0, (utcnow() - run.paused_at).total_seconds())
    return {"paused_total_s": run.paused_total_s + extra, "paused_at": None}
