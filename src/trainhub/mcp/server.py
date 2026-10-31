"""MCP server (FastMCP): exposes to an agent the same actions as the interface.

It only calls the service layer; a run launched by an agent shows up immediately in the
interface (``run.created`` event) with ``source = "mcp"``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.logs_clean import readable_logs
from ..core.models import ExternalRun, MlflowInfo, Run, RunSource, RunStatus
from ..core.status import elapsed_seconds, format_duration, paused_seconds
from ..service import RunService, ServiceError, allowed_actions
from ..service.schemas import MlflowOrphanListing, Selection, SeriesRequest

INSTRUCTIONS = """\
TrainHub launches and monitors training commands on remote GPU machines
(ssh → docker → venv → command). A run is defined by 5 choices: project, machine, Docker
image, Python venv and config file. Recommended flow: list_projects, then
list_resources and list_configs for a project, preview_command to check, launch_run,
then wait_run / get_logs to monitor (get_gpus helps choosing a machine and
cuda_visible_devices). Projects whose command contains {checkpoint} (accepts_checkpoint,
e.g. inference) take a checkpoint path: find one with list_checkpoint_sources and
list_checkpoints. Statuses: launching, running, paused, stopping,
succeeded, failed, stopped, unreachable (machine unreachable, actual status unknown).
To run the same command several times in a row (e.g. with different seeds), pass series
to launch_run: one run, one container, the repetitions one after the other.
A failed run (unseen_failure) stays among the active runs until mark_run_seen (also for
a stalled external run). Projects
tracked with MLflow also show external runs: MLflow runs TrainHub did not launch, found in
the experiments the project names (list_external_runs); TrainHub only reads them.
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

LAUNCH_WAIT_S = 15.0
STOP_WAIT_S = 15.0
MAX_LOG_CHARS = 12_000

ProjectArg = Annotated[str, Field(description="Project id (see list_projects)")]
RunIdArg = Annotated[str, Field(description="Short run id, e.g. r7f3k2")]
MachineArg = Annotated[str, Field(description="Machine id (see list_resources)")]
ImageArg = Annotated[str, Field(description="Docker image id (see list_resources)")]
VenvArg = Annotated[str, Field(description="Venv id (see list_resources)")]
ConfigArg = Annotated[str, Field(description="Config identifier (see list_configs)")]
OverridesArg = Annotated[
    list[str] | None,
    Field(
        description="Optional extra arguments appended to the command, "
        "e.g. ['trainer.max_epochs=5', 'seed=2']"
    ),
]
SinceArg = Annotated[
    int | None,
    Field(description="Log cursor returned by an earlier get_logs / wait_run", ge=0),
]
CudaArg = Annotated[str | None, Field(description="Optional CUDA_VISIBLE_DEVICES, e.g. '0,1'")]
ParamsArg = Annotated[
    dict[str, str] | None,
    Field(
        description="Values of the free parameters of the project command ({name} other than "
        "config, config_name, run_id, machine, project; see list_projects). Each value is "
        "split as a shell would; missing or empty: nothing is inserted."
    ),
]
CheckpointArg = Annotated[
    str | None,
    Field(
        description="Absolute path of a checkpoint on the machine (root of its source + path "
        "from list_checkpoints), for a project whose command contains {checkpoint}"
    ),
]
SeriesArg = Annotated[
    SeriesRequest | None,
    Field(
        description="Optional: repeat the run count times in a row, in the same container "
        "(a single run). random: free parameters drawn at random for each repetition, e.g. "
        '{"seed": {"min": 0, "max": 99999}}; the series stops at the first failure unless '
        "continue_on_failure. Each repetition gets TRAINHUB_SERIES_INDEX (1, 2…)."
    ),
]


def series_summary(run: Run) -> dict[str, Any] | None:
    s = run.series
    if s is None:
        return None
    return {
        "count": s.count,
        "current": s.current,
        "exit_codes": s.exit_codes,
        "values": s.values,
        "continue_on_failure": s.continue_on_failure,
    }


def mlflow_summary(info: MlflowInfo | None) -> dict[str, Any] | None:
    """What a model needs of an MLflow run."""
    if info is None:
        return None
    return {
        "run_id": info.run_id,
        "status": info.status,
        "epoch": info.epoch,
        "max_epochs": info.max_epochs,
        "stale": info.stale,
        "last_activity": info.last_activity.isoformat() if info.last_activity else None,
        "url": info.url,
        "error": info.error,
    }


def external_summary(run: ExternalRun) -> dict[str, Any]:
    """Compact view of an external run (MLflow run not launched by TrainHub)."""
    ml = run.mlflow
    return {
        "id": run.id,
        "project": run.project_id,
        "name": run.name,
        "user": run.user,
        "experiment": run.experiment,
        "active": run.is_active,
        "failed": run.is_failure,
        "unseen_failure": run.unseen_failure,
        "stale_seen": run.stale_seen_at is not None,
        "started_at": ml.started_at.isoformat() if ml.started_at else None,
        "finished_at": ml.finished_at.isoformat() if ml.finished_at else None,
        "mlflow": mlflow_summary(ml),
    }


def run_summary(run: Run) -> dict[str, Any]:
    """Compact view of a run, readable by a model."""
    return {
        "id": run.id,
        "project": run.project_id,
        "machine": run.machine_id,
        "image": run.image_id,
        "venv": run.venv_id,
        "config": run.config,
        "status": str(run.status),
        "elapsed": format_duration(elapsed_seconds(run)),
        "paused": format_duration(paused_seconds(run)) if paused_seconds(run) else None,
        "exit_code": run.exit_code,
        "error": run.error,
        "source": str(run.source),
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "container_alive": run.container_alive,
        "allowed_actions": allowed_actions(run.status, container_alive=run.container_alive),
        "series": series_summary(run),
        "failed": run.is_failure,
        "unseen_failure": run.unseen_failure,
        "mlflow": mlflow_summary(run.mlflow),
    }


def run_detail(run: Run) -> dict[str, Any]:
    d = run_summary(run)
    d.update(
        command=run.command,
        full_command=run.full_command,
        container_name=run.container_name,
        overrides=run.overrides,
        cuda_visible_devices=run.cuda_visible_devices,
        checkpoint=run.checkpoint,
        elapsed_s=round(elapsed_seconds(run), 1),
        paused_total_s=round(paused_seconds(run), 1),
    )
    return d


def build_mcp(
    svc: RunService, *, readonly: bool = False, dns_rebinding_protection: bool = False
) -> FastMCP:
    """Build the MCP server; ``readonly`` only exposes the read tools."""
    mcp = FastMCP(
        "trainhub",
        instructions=INSTRUCTIONS,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=dns_rebinding_protection
        ),
    )

    async def call(coro: Any) -> Any:
        try:
            return await coro
        except ServiceError as exc:
            raise ToolError(exc.message) from exc

    # -- reads ---------------------------------------------------------------------

    @mcp.tool(
        annotations=READ,
        description="List the projects (tabs) with their current generic command, "
        "the allowed resources (machines, images, venvs) and the static configs.",
    )
    async def list_projects() -> list[dict[str, Any]]:
        out = []
        for p in svc.inventory.projects:
            v = svc.project_view(p)
            out.append(
                {
                    "id": v.id,
                    "label": v.label,
                    "command": v.command,
                    "command_overridden": v.command_overridden,
                    "params": v.params,
                    "accepts_checkpoint": v.accepts_checkpoint,
                    "machines": v.machines,
                    "images": v.images,
                    "venvs": v.venvs,
                    "static_configs": v.static_configs,
                    "discovers_configs": v.discover,
                }
            )
        return out

    @mcp.tool(
        annotations=READ,
        description="Machines, images and venvs allowed for a project, with machine "
        "reachability (reachable: true/false/null if not checked yet) and "
        "the images compatible with each venv.",
    )
    async def list_resources(project: ProjectArg) -> dict[str, Any]:
        view = await call(_project_view(svc, project))
        inv = svc.inventory
        return {
            "project": project,
            "machines": [
                {
                    "id": m,
                    "label": inv.machine(m).label,
                    "host": inv.machine(m).host,
                    "reachable": svc.machine_state(m).reachable,
                    "error": svc.machine_state(m).error,
                }
                for m in view.machines
            ],
            "images": [{"id": i, "image": inv.image(i).image} for i in view.images],
            "venvs": [
                {"id": v, "path": inv.venv(v).path, "images": view.venv_images.get(v)}
                for v in view.venvs
            ],
        }

    @mcp.tool(
        annotations=READ,
        description="Configs available for a project. A config is the path of the "
        "YAML file relative to the project's configs root, without extension (e.g. "
        "'experiments/unet_large'), discovered recursively on the machine (first allowed "
        "machine if omitted; 60 s cache). run_counts gives, for configs already used, "
        "the number of runs launched (total) and in progress (active).",
    )
    async def list_configs(
        project: ProjectArg,
        machine: Annotated[str | None, Field(description="Machine used for discovery")] = None,
    ) -> dict[str, Any]:
        cl = await call(svc.get_configs(project, machine))
        return {
            "project": project,
            "machine": cl.machine,
            "root": cl.root,
            "configs": cl.configs,
            "run_counts": {k: v.model_dump() for k, v in cl.run_counts.items()},
            "discover_error": cl.discover_error,
        }

    @mcp.tool(
        annotations=READ,
        description="Build the full command (ssh → docker run → venv → command) "
        "for a selection, without running anything (dry run). Useful before launch_run.",
    )
    async def preview_command(
        project: ProjectArg,
        machine: MachineArg,
        image: ImageArg,
        venv: VenvArg,
        config: ConfigArg,
        overrides: OverridesArg = None,
        cuda_visible_devices: CudaArg = None,
        params: ParamsArg = None,
        series: SeriesArg = None,
        checkpoint: CheckpointArg = None,
    ) -> dict[str, Any]:
        sel = _selection(
            project,
            machine,
            image,
            venv,
            config,
            overrides,
            cuda_visible_devices,
            params,
            series,
            checkpoint,
        )
        p = await call(svc.preview(sel))
        return {"command": p.command, "full_command": p.text, "exact": p.exact}

    @mcp.tool(
        annotations=READ,
        description="Checkpoint sources: directories of runs on a machine (launched by "
        "TrainHub or not), with the total size and count of checkpoints of the last scan "
        "(null: not scanned yet).",
    )
    async def list_checkpoint_sources() -> list[dict[str, Any]]:
        return [v.model_dump(mode="json") for v in svc.checkpoints.sources()]

    @mcp.tool(
        annotations=READ,
        description="Runs and checkpoints of a source, most recent run first: for each run "
        "its groups (e.g. topk, last) and checkpoint files (path relative to the root, "
        "size in bytes, date, inference runs already launched with it), its MLflow runs and "
        "the inferences made with its checkpoints (TrainHub and MLflow). The absolute path "
        "to pass as checkpoint is root + '/' + path. Scan cached 5 min (refresh to force).",
    )
    async def list_checkpoints(
        source: Annotated[str, Field(description="Source id (see list_checkpoint_sources)")],
        name: Annotated[
            str | None, Field(description="Only runs whose name contains this text")
        ] = None,
        limit: Annotated[int, Field(description="Maximum number of runs", ge=1, le=500)] = 30,
        refresh: Annotated[bool, Field(description="Rescan the disk")] = False,
    ) -> dict[str, Any]:
        lst = await call(svc.checkpoints.listing(source, refresh=refresh))
        runs = [r for r in lst.runs if not name or name.lower() in r.name.lower()]

        def file(f: Any) -> dict[str, Any]:
            d = {"path": f.path, "size": f.size, "mtime": f.mtime.isoformat()}
            if f.runs:
                d["runs"] = f.runs.model_dump()
            return d

        return {
            "source": lst.source,
            "machine": lst.machine,
            "root": lst.root,
            "size": lst.size,
            "count": lst.count,
            "error": lst.error,
            "runs_total": len(runs),
            "runs": [
                {
                    "name": r.name,
                    "size": r.size,
                    "mtime": r.mtime.isoformat() if r.mtime else None,
                    "groups": {g.name: [file(f) for f in g.files] for g in r.groups},
                    **({"others": [file(f) for f in r.others]} if r.others else {}),
                    **(
                        {"mlflow_runs": [m.model_dump(mode="json") for m in r.mlflow_runs]}
                        if r.mlflow_runs
                        else {}
                    ),
                    **(
                        {
                            "inferences": [
                                i.model_dump(mode="json", exclude_none=True) for i in r.inferences
                            ]
                        }
                        if r.inferences
                        else {}
                    ),
                }
                for r in runs[:limit]
            ],
        }

    @mcp.tool(
        annotations=READ,
        description="MLflow runs without a directory on disk: runs of the checkpoint sources "
        "with mlflow.match whose logged directory exists in no source (deleted, or written "
        "elsewhere). scan_errors lists sources that could not be scanned: their runs may be "
        "reported by mistake. Cached 5 min (refresh to force).",
    )
    async def list_mlflow_orphans(
        limit: Annotated[int, Field(description="Maximum number of runs", ge=1, le=1000)] = 100,
        refresh: Annotated[bool, Field(description="Rescan the disks and MLflow")] = False,
    ) -> dict[str, Any]:
        lst: MlflowOrphanListing = await call(svc.checkpoints.mlflow_orphans(refresh=refresh))
        out: dict[str, Any] = lst.model_dump(mode="json", exclude={"runs"})
        out["runs_total"] = len(lst.runs)
        out["runs"] = [o.model_dump(mode="json", exclude_none=True) for o in lst.runs[:limit]]
        return out

    @mcp.tool(
        annotations=READ,
        description="List the runs, most recent first, with status and duration. "
        "status: a status (launching, running, paused, stopping, succeeded, failed, "
        "stopped, unreachable) or 'active' / 'finished'.",
    )
    async def list_runs(
        project: Annotated[str | None, Field(description="Filter by project")] = None,
        status: Annotated[str | None, Field(description="Filter by status")] = None,
        limit: Annotated[int, Field(description="Maximum number of runs", ge=1, le=500)] = 50,
    ) -> list[dict[str, Any]]:
        statuses: list[RunStatus] | None = None
        if status:
            if status == "active":
                statuses = [s for s in RunStatus if s.is_active]
            elif status == "finished":
                statuses = [s for s in RunStatus if s.is_finished]
            else:
                try:
                    statuses = [RunStatus(status)]
                except ValueError as exc:
                    raise ToolError(
                        f"unknown status: {status!r}; possible: "
                        f"{', '.join(s.value for s in RunStatus)}, active, finished"
                    ) from exc
        runs = await call(svc.list_runs(project=project, status=statuses, limit=limit))
        return [run_summary(r) for r in runs]

    @mcp.tool(
        annotations=READ,
        description="Full detail of a run: status, durations, exit code, error, "
        "resolved command and executed argv, possible actions.",
    )
    async def get_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_detail(await call(svc.get_run(run_id)))

    @mcp.tool(
        annotations=READ,
        description="Logs of a run as readable text: ANSI sequences stripped, progress "
        "bars reduced to their last state, last lines only (tail, default 200), optional "
        "regex filtering (grep, case-insensitive). Output truncated if too long. The header "
        "gives a cursor: pass it as since on the next call to only get the new output.",
    )
    async def get_logs(
        run_id: RunIdArg,
        tail: Annotated[int, Field(description="Number of final lines", ge=1, le=5000)] = 200,
        grep: Annotated[str | None, Field(description="Regex filtering the lines")] = None,
        since: SinceArg = None,
    ) -> str:
        run = await call(svc.get_run(run_id))
        sl = await call(svc.read_logs_since(run_id, since or 0))
        text = readable_logs(sl.data, tail=tail, grep=grep, max_chars=MAX_LOG_CHARS)
        header = (
            f"[run {run.id} · {run.status} · {format_duration(elapsed_seconds(run))}"
            f" · cursor {sl.cursor}]\n"
        )
        if since and sl.reset:
            header += "[logs rewritten since this cursor: read from the start]\n"
        empty = "(no new output)" if since and not sl.reset else "(no lines)"
        return header + (text if text.strip() else empty)

    @mcp.tool(
        annotations=READ,
        description="Wait, without polling, until a run finishes, a log line matches pattern "
        "(case-insensitive regex, searched in the output written after since, default: "
        "from now) or timeout_s expires; with until='status_change', also return on any status "
        "change (e.g. launching → running, running → unreachable). Returns the run, "
        "wait.reason (finished, match, status_change, timeout), the matching line, a log "
        "cursor for get_logs / the next wait_run, and the last log lines. Keep timeout_s "
        "below your client's tool-call timeout; call again to keep waiting.",
    )
    async def wait_run(
        run_id: RunIdArg,
        ctx: Context,  # type: ignore[type-arg]
        timeout_s: Annotated[
            float, Field(description="Maximum wait in seconds (0: check once)", ge=0, le=3600)
        ] = 120,
        pattern: Annotated[
            str | None, Field(description="Regex to wait for in the logs, e.g. 'epoch 3'")
        ] = None,
        since: SinceArg = None,
        until: Annotated[
            Literal["finished", "status_change"],
            Field(description="finished (default) or status_change"),
        ] = "finished",
        log_tail: Annotated[
            int, Field(description="Last log lines returned (0: none)", ge=0, le=500)
        ] = 20,
    ) -> dict[str, Any]:
        async def tick(elapsed: float, run: Run) -> None:
            await ctx.report_progress(
                elapsed, timeout_s, f"run {run.id} {run.status}, waited {elapsed:.0f} s"
            )

        res = await call(
            svc.wait_run(
                run_id, timeout=timeout_s, pattern=pattern, since=since, until=until, on_tick=tick
            )
        )
        out = run_summary(res.run)
        sl = await call(svc.read_logs_since(run_id))
        out["wait"] = {
            "reason": res.reason,
            "waited_s": round(res.waited_s, 1),
            "match": res.match,
            "cursor": res.cursor if res.cursor is not None else sl.cursor,
        }
        if log_tail:
            out["last_lines"] = readable_logs(sl.data, tail=log_tail, max_chars=4000)
        return out

    @mcp.tool(
        annotations=READ,
        description="GPU state of machines (nvidia-smi on the host): memory used/total, "
        "utilization, temperature, number of compute processes (all users) and the active "
        "TrainHub runs pinned to each GPU by CUDA_VISIBLE_DEVICES; active_runs also lists "
        "runs without restriction (cuda_visible_devices null: every GPU visible). Use it to "
        "pick a machine and cuda_visible_devices before launch_run. Without machine: every "
        "machine of the project (or of the inventory), queried in parallel.",
    )
    async def get_gpus(
        machine: Annotated[str | None, Field(description="Machine id")] = None,
        project: Annotated[
            str | None, Field(description="Without machine: the machines of this project")
        ] = None,
    ) -> list[dict[str, Any]]:
        if machine is not None:
            ids = [machine]
        elif project is not None:
            ids = (await call(_project_view(svc, project))).machines
        else:
            ids = [m.id for m in svc.inventory.machines]

        async def one(mid: str) -> dict[str, Any]:
            try:
                return (await svc.machine_gpus(mid)).model_dump(mode="json")
            except ServiceError as exc:
                if machine is not None:
                    raise ToolError(exc.message) from exc
                return {"machine": mid, "gpus": [], "active_runs": [], "error": exc.message}

        return list(await asyncio.gather(*(one(m) for m in ids)))

    @mcp.tool(
        annotations=READ,
        description="Load of the inventory machines: CPU (usage of every core over 0.5 s, "
        "load average, memory, from /proc) and GPUs (as get_gpus). An unreachable machine "
        "has reachable false. Without machine: every machine, queried in parallel.",
    )
    async def get_machine_load(
        machine: Annotated[str | None, Field(description="Machine id")] = None,
    ) -> list[dict[str, Any]]:
        ids = [machine] if machine is not None else [m.id for m in svc.inventory.machines]
        loads = await asyncio.gather(*(call(svc.machine_load(m)) for m in ids))
        return [x.model_dump(mode="json") for x in loads]

    @mcp.tool(
        annotations=READ,
        description="External runs: MLflow runs that TrainHub did not launch, found in the "
        "experiments a project names in its mlflow settings (a training started by hand, from "
        "a notebook...). TrainHub knows only what MLflow logged: no machine, container nor "
        "logs, and no action but mark_run_seen and delete_external_run. Most recent first; "
        "mlflow.stale: running but no new metric for a while.",
    )
    async def list_external_runs(
        project: Annotated[str | None, Field(description="Filter by project")] = None,
    ) -> list[dict[str, Any]]:
        return [external_summary(r) for r in await call(svc.list_external_runs(project))]

    if readonly:
        return mcp

    # -- actions ----------------------------------------------------------------------

    @mcp.tool(
        annotations=WRITE,
        description="Launch a run (docker run -d on the machine). Waits up to 15 s for the "
        "container to start then returns the run (status running, launching if the image is "
        "still being pulled, or failed with the error). The run shows up immediately in "
        "the interface.",
    )
    async def launch_run(
        project: ProjectArg,
        machine: MachineArg,
        image: ImageArg,
        venv: VenvArg,
        config: ConfigArg,
        overrides: OverridesArg = None,
        cuda_visible_devices: CudaArg = None,
        params: ParamsArg = None,
        series: SeriesArg = None,
        checkpoint: CheckpointArg = None,
    ) -> dict[str, Any]:
        sel = _selection(
            project,
            machine,
            image,
            venv,
            config,
            overrides,
            cuda_visible_devices,
            params,
            series,
            checkpoint,
        )
        run = await call(svc.launch(sel, source=RunSource.MCP, wait=LAUNCH_WAIT_S))
        return run_summary(run)

    @mcp.tool(
        annotations=WRITE,
        description="Pause a run (docker pause; status running required). Processes "
        "are frozen but GPU memory is not released.",
    )
    async def pause_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.pause(run_id)))

    @mcp.tool(
        annotations=WRITE,
        description="Resume a paused run (docker unpause; status paused required).",
    )
    async def resume_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.resume(run_id)))

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description="Stop a run (docker stop: SIGTERM then SIGKILL after the project's "
        "timeout; status running or paused required). Waits up to 15 s; the final status "
        "is stopped.",
    )
    async def stop_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.stop(run_id, wait=STOP_WAIT_S)))

    @mcp.tool(
        annotations=WRITE,
        description="Mark an active run as finished (status failed, explicit error) without "
        "running anything on the machine: for a run stuck active while its machine is down "
        "or its container is dead. If the container turns out to be running when the "
        "machine answers again, the run shows as running again. Use stop_run for a run "
        "that is really running.",
    )
    async def force_finish_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.force_finish(run_id)))

    @mcp.tool(
        annotations=WRITE,
        description="Restart the same container (docker start, or docker restart if it was "
        "kept started): the command starts over. Finished status required "
        "(succeeded, failed, stopped).",
    )
    async def restart_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.restart(run_id)))

    @mcp.tool(
        annotations=WRITE,
        description="Create a new run with the same parameters (project, machine, image, "
        "venv, config, overrides, series with the same values) and the current project "
        "command. Finished status required.",
    )
    async def relaunch_run(run_id: RunIdArg) -> dict[str, Any]:
        run = await call(svc.relaunch(run_id, source=RunSource.MCP, wait=LAUNCH_WAIT_S))
        return run_summary(run)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description="Delete a finished run and its container (docker rm -f); the log "
        "backup file is kept by TrainHub. Finished status required.",
    )
    async def delete_run(run_id: RunIdArg) -> dict[str, Any]:
        await call(svc.delete(run_id))
        return {"deleted": run_id}

    @mcp.tool(
        annotations=WRITE,
        description="Close (docker stop) the kept container of a finished run "
        "(container_alive=true). The run status does not change.",
    )
    async def close_container(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.close_container(run_id)))

    @mcp.tool(
        annotations=WRITE,
        description="Mark a run as seen: a failed run (unseen_failure=true stays among the "
        "active runs until then), or a stalled external run (still RUNNING in MLflow but "
        "mlflow.stale: no new metric for a while, often a process killed without MLflow "
        "knowing). It moves to the finished runs; a stalled one comes back if it logs again. "
        "Takes a TrainHub run id or an external run id (MLflow run id).",
    )
    async def mark_run_seen(
        run_id: Annotated[str, Field(description="TrainHub run id, or MLflow run id")],
    ) -> dict[str, Any]:
        if await svc.store.get_run(run_id) is not None:
            return run_summary(await call(svc.mark_seen(run_id)))
        return external_summary(await call(svc.mark_external_seen(run_id)))

    @mcp.tool(
        annotations=WRITE,
        description="Remove an external run from TrainHub, running or not: it is no longer "
        "shown and does not come back. Nothing changes in MLflow.",
    )
    async def delete_external_run(
        run_id: Annotated[str, Field(description="External run id (MLflow run id)")],
    ) -> dict[str, Any]:
        await call(svc.delete_external(run_id))
        return {"deleted": run_id}

    @mcp.tool(
        annotations=WRITE,
        description="Change the generic command of a project (persisted; only affects "
        "runs launched afterwards). Placeholders: {config} (required), {run_id}, "
        "{machine}, {project}. A command identical to the YAML one cancels the override.",
    )
    async def set_project_command(
        project: ProjectArg,
        command: Annotated[
            str, Field(description="New command, e.g. python train.py --config-name {config}")
        ],
    ) -> dict[str, Any]:
        v = await call(svc.set_project_command(project, command))
        return {"project": v.id, "command": v.command, "command_overridden": v.command_overridden}

    return mcp


async def _project_view(svc: RunService, project: str) -> Any:
    return svc.project_view(svc._project(project))


def _selection(
    project: str,
    machine: str,
    image: str,
    venv: str,
    config: str,
    overrides: list[str] | None,
    cuda: str | None,
    params: dict[str, str] | None = None,
    series: SeriesRequest | None = None,
    checkpoint: str | None = None,
) -> Selection:
    return Selection(
        project=project,
        machine=machine,
        image=image,
        venv=venv,
        config=config,
        overrides=overrides or [],
        cuda_visible_devices=cuda,
        params=params or {},
        series=series,
        checkpoint=checkpoint,
    )


READ_TOOLS = frozenset(
    {
        "list_projects",
        "list_resources",
        "list_configs",
        "preview_command",
        "list_runs",
        "get_run",
        "get_logs",
        "wait_run",
        "get_gpus",
        "list_checkpoint_sources",
        "list_checkpoints",
        "list_mlflow_orphans",
        "get_machine_load",
        "list_external_runs",
    }
)
