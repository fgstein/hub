"""MCP server (FastMCP): exposes to an agent the same actions as the interface.

It only calls the service layer; a run launched by an agent shows up immediately in the
interface (``run.created`` event) with ``source = "mcp"``.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from ..core.logs_clean import readable_logs
from ..core.models import Run, RunSource, RunStatus
from ..core.status import elapsed_seconds, format_duration, paused_seconds
from ..service import RunService, ServiceError, allowed_actions
from ..service.schemas import Selection

INSTRUCTIONS = """\
TrainHub launches and monitors Hydra trainings on remote GPU machines
(ssh → docker → venv → command). A run is defined by 5 choices: project, machine, Docker
image, Python venv and Hydra config. Recommended flow: list_projects, then
list_resources and list_configs for a project, preview_command to check, launch_run,
then get_run / get_logs to monitor. Statuses: launching, running, paused, stopping,
succeeded, failed, stopped, unreachable (machine unreachable, actual status unknown).
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
ConfigArg = Annotated[str, Field(description="Hydra config name (see list_configs)")]
OverridesArg = Annotated[
    list[str] | None,
    Field(description="Optional Hydra overrides, e.g. ['trainer.max_epochs=5', 'seed=2']"),
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
    }


def run_detail(run: Run) -> dict[str, Any]:
    d = run_summary(run)
    d.update(
        command=run.command,
        full_command=run.full_command,
        container_name=run.container_name,
        overrides=run.overrides,
        cuda_visible_devices=run.cuda_visible_devices,
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
        description="Hydra configs available for a project. A config is the path of the "
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
    ) -> dict[str, Any]:
        sel = _selection(
            project, machine, image, venv, config, overrides, cuda_visible_devices, params
        )
        p = await call(svc.preview(sel))
        return {"command": p.command, "full_command": p.text, "exact": p.exact}

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
        "regex filtering (grep, case-insensitive). Output truncated if too long.",
    )
    async def get_logs(
        run_id: RunIdArg,
        tail: Annotated[int, Field(description="Number of final lines", ge=1, le=5000)] = 200,
        grep: Annotated[str | None, Field(description="Regex used to filter lines")] = None,
    ) -> str:
        run = await call(svc.get_run(run_id))
        raw = await call(svc.read_logs(run_id, None if grep else max(tail * 2, 500)))
        text = readable_logs(raw, tail=tail, grep=grep, max_chars=MAX_LOG_CHARS)
        header = f"[run {run.id} · {run.status} · {format_duration(elapsed_seconds(run))}]\n"
        return header + (text if text.strip() else "(no lines)")

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
    ) -> dict[str, Any]:
        sel = _selection(
            project, machine, image, venv, config, overrides, cuda_visible_devices, params
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
        description="Restart the same container (docker start, or docker restart if it was "
        "kept started): the command starts over. Finished status required "
        "(succeeded, failed, stopped).",
    )
    async def restart_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.restart(run_id)))

    @mcp.tool(
        annotations=WRITE,
        description="Create a new run with the same parameters (project, machine, image, "
        "venv, config, overrides) and the current project command. Finished status required.",
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
    }
)
