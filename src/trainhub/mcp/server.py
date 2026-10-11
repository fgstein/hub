"""Serveur MCP (FastMCP) : expose à un agent les mêmes actions que l'interface.

Il appelle uniquement la couche service ; un run lancé par un agent apparaît immédiatement
dans l'interface (événement ``run.created``) avec ``source = "mcp"``.
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
TrainHub lance et suit des entraînements Hydra sur des machines GPU distantes
(ssh → docker → venv → commande). Un run est défini par 5 choix : projet, machine, image
Docker, venv Python et config Hydra. Démarche conseillée : list_projects, puis
list_resources et list_configs pour un projet, preview_command pour vérifier, launch_run,
puis get_run / get_logs pour suivre. Statuts : launching, running, paused, stopping,
succeeded, failed, stopped, unreachable (machine injoignable, statut réel inconnu).
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

LAUNCH_WAIT_S = 15.0
STOP_WAIT_S = 15.0
MAX_LOG_CHARS = 12_000

ProjectArg = Annotated[str, Field(description="Id du projet (voir list_projects)")]
RunIdArg = Annotated[str, Field(description="Id court du run, ex. r7f3k2")]
MachineArg = Annotated[str, Field(description="Id de la machine (voir list_resources)")]
ImageArg = Annotated[str, Field(description="Id de l'image Docker (voir list_resources)")]
VenvArg = Annotated[str, Field(description="Id du venv (voir list_resources)")]
ConfigArg = Annotated[str, Field(description="Nom de la config Hydra (voir list_configs)")]
OverridesArg = Annotated[
    list[str] | None,
    Field(description="Overrides Hydra facultatifs, ex. ['trainer.max_epochs=5', 'seed=2']"),
]
CudaArg = Annotated[str | None, Field(description="CUDA_VISIBLE_DEVICES facultatif, ex. '0,1'")]


def run_summary(run: Run) -> dict[str, Any]:
    """Vue compacte d'un run, lisible par un modèle."""
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
        "allowed_actions": allowed_actions(run.status),
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
    """Construit le serveur MCP ; ``readonly`` n'expose que les outils de lecture."""
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

    # -- lectures ------------------------------------------------------------------

    @mcp.tool(
        annotations=READ,
        description="Liste les projets (onglets) avec leur commande générique courante, "
        "les ressources autorisées (machines, images, venvs) et les configs statiques.",
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
        description="Machines, images et venvs autorisés pour un projet, avec la "
        "joignabilité des machines (reachable: true/false/null si pas encore vérifiée) et "
        "les images compatibles avec chaque venv.",
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
        description="Configs Hydra disponibles pour un projet : liste statique et/ou "
        "découverte par ls sur la machine (première machine autorisée si omise ; cache 60 s).",
    )
    async def list_configs(
        project: ProjectArg,
        machine: Annotated[str | None, Field(description="Machine pour la découverte")] = None,
    ) -> dict[str, Any]:
        cl = await call(svc.get_configs(project, machine))
        return {
            "project": project,
            "machine": cl.machine,
            "configs": cl.configs,
            "discover_error": cl.discover_error,
        }

    @mcp.tool(
        annotations=READ,
        description="Construit la commande complète (ssh → docker run → venv → commande) "
        "pour une sélection, sans rien exécuter (dry run). Utile avant launch_run.",
    )
    async def preview_command(
        project: ProjectArg,
        machine: MachineArg,
        image: ImageArg,
        venv: VenvArg,
        config: ConfigArg,
        overrides: OverridesArg = None,
        cuda_visible_devices: CudaArg = None,
    ) -> dict[str, Any]:
        sel = _selection(project, machine, image, venv, config, overrides, cuda_visible_devices)
        p = await call(svc.preview(sel))
        return {"command": p.command, "full_command": p.text, "exact": p.exact}

    @mcp.tool(
        annotations=READ,
        description="Liste les runs, du plus récent au plus ancien, avec statut et durée. "
        "status : un statut (launching, running, paused, stopping, succeeded, failed, "
        "stopped, unreachable) ou 'active' / 'finished'.",
    )
    async def list_runs(
        project: Annotated[str | None, Field(description="Filtrer par projet")] = None,
        status: Annotated[str | None, Field(description="Filtrer par statut")] = None,
        limit: Annotated[int, Field(description="Nombre maximal de runs", ge=1, le=500)] = 50,
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
                        f"statut inconnu : {status!r} ; possibles : "
                        f"{', '.join(s.value for s in RunStatus)}, active, finished"
                    ) from exc
        runs = await call(svc.list_runs(project=project, status=statuses, limit=limit))
        return [run_summary(r) for r in runs]

    @mcp.tool(
        annotations=READ,
        description="Détail complet d'un run : statut, durées, code de sortie, erreur, "
        "commande résolue et argv exécuté, actions possibles.",
    )
    async def get_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_detail(await call(svc.get_run(run_id)))

    @mcp.tool(
        annotations=READ,
        description="Logs d'un run en texte lisible : séquences ANSI supprimées, barres de "
        "progression réduites à leur dernier état, dernières lignes seulement (tail, défaut "
        "200), filtrage facultatif par regex (grep, insensible à la casse). Sortie tronquée "
        "si trop longue.",
    )
    async def get_logs(
        run_id: RunIdArg,
        tail: Annotated[int, Field(description="Nombre de lignes finales", ge=1, le=5000)] = 200,
        grep: Annotated[str | None, Field(description="Regex de filtrage des lignes")] = None,
    ) -> str:
        run = await call(svc.get_run(run_id))
        raw = await call(svc.read_logs(run_id, None if grep else max(tail * 2, 500)))
        text = readable_logs(raw, tail=tail, grep=grep, max_chars=MAX_LOG_CHARS)
        header = f"[run {run.id} · {run.status} · {format_duration(elapsed_seconds(run))}]\n"
        return header + (text if text.strip() else "(aucune ligne)")

    if readonly:
        return mcp

    # -- actions ----------------------------------------------------------------------

    @mcp.tool(
        annotations=WRITE,
        description="Lance un run (docker run -d sur la machine). Attend jusqu'à 15 s le "
        "démarrage du conteneur puis renvoie le run (statut running, launching si l'image se "
        "télécharge encore, ou failed avec l'erreur). Le run apparaît aussitôt dans "
        "l'interface.",
    )
    async def launch_run(
        project: ProjectArg,
        machine: MachineArg,
        image: ImageArg,
        venv: VenvArg,
        config: ConfigArg,
        overrides: OverridesArg = None,
        cuda_visible_devices: CudaArg = None,
    ) -> dict[str, Any]:
        sel = _selection(project, machine, image, venv, config, overrides, cuda_visible_devices)
        run = await call(svc.launch(sel, source=RunSource.MCP, wait=LAUNCH_WAIT_S))
        return run_summary(run)

    @mcp.tool(
        annotations=WRITE,
        description="Met un run en pause (docker pause ; statut running requis). Les "
        "processus sont gelés mais la mémoire GPU n'est pas libérée.",
    )
    async def pause_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.pause(run_id)))

    @mcp.tool(
        annotations=WRITE,
        description="Reprend un run en pause (docker unpause ; statut paused requis).",
    )
    async def resume_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.resume(run_id)))

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description="Arrête un run (docker stop : SIGTERM puis SIGKILL après le délai du "
        "projet ; statut running ou paused requis). Attend jusqu'à 15 s ; le statut final "
        "est stopped.",
    )
    async def stop_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.stop(run_id, wait=STOP_WAIT_S)))

    @mcp.tool(
        annotations=WRITE,
        description="Redémarre le même conteneur (docker start) : la commande repart du "
        "début. Statut terminé requis (succeeded, failed, stopped).",
    )
    async def restart_run(run_id: RunIdArg) -> dict[str, Any]:
        return run_summary(await call(svc.restart(run_id)))

    @mcp.tool(
        annotations=WRITE,
        description="Crée un nouveau run avec les mêmes paramètres (projet, machine, image, "
        "venv, config, overrides) et la commande actuelle du projet. Statut terminé requis.",
    )
    async def relaunch_run(run_id: RunIdArg) -> dict[str, Any]:
        run = await call(svc.relaunch(run_id, source=RunSource.MCP, wait=LAUNCH_WAIT_S))
        return run_summary(run)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description="Supprime un run terminé et son conteneur (docker rm) : ses logs sont "
        "perdus. Statut terminé requis.",
    )
    async def delete_run(run_id: RunIdArg) -> dict[str, Any]:
        await call(svc.delete(run_id))
        return {"deleted": run_id}

    @mcp.tool(
        annotations=WRITE,
        description="Change la commande générique d'un projet (persistée ; n'affecte que "
        "les runs lancés ensuite). Placeholders : {config} (obligatoire), {run_id}, "
        "{machine}, {project}. Une commande identique au YAML annule la surcharge.",
    )
    async def set_project_command(
        project: ProjectArg,
        command: Annotated[
            str, Field(description="Nouvelle commande, ex. python train.py --config-name {config}")
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
) -> Selection:
    return Selection(
        project=project,
        machine=machine,
        image=image,
        venv=venv,
        config=config,
        overrides=overrides or [],
        cuda_visible_devices=cuda,
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
