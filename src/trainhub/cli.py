"""CLI minimale : ``trainhub launch|ps|logs|stop|pause|resume|rm|preview|serve|mcp``.

Les commandes de gestion des runs s'appuient sur la classe :class:`trainhub.TrainHub`
(même couche service que le serveur, même base SQLite). Elle sert aussi d'outil de débogage.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from typing import Any

from .core.commands import preview, preview_exact
from .core.ids import new_run_id
from .core.inventory import InventoryError, load_inventory
from .core.logs_clean import readable_logs
from .core.models import Run, RunStatus, UnknownResourceError
from .core.selection import make_spec
from .core.status import elapsed_seconds, format_duration


def _inventory_path(args: argparse.Namespace) -> str:
    return str(args.inventory or os.environ.get("TRAINHUB_INVENTORY", "inventory.yaml"))


def _hub(args: argparse.Namespace) -> Any:
    from .client import TrainHub

    return TrainHub.from_inventory(
        _inventory_path(args),
        executor=args.executor,
        data_dir=args.data_dir,
        monitor=False,
    )


def cmd_preview(args: argparse.Namespace) -> int:
    inv = load_inventory(_inventory_path(args))
    spec = make_spec(
        inv,
        run_id=args.run_id or new_run_id(),
        project=args.project,
        machine=args.machine,
        image=args.image,
        venv=args.venv,
        config=args.config,
        command=args.command,
        overrides=args.overrides,
        cuda_visible_devices=args.gpus,
        params=_params(args),
    )
    print(preview_exact(spec) if args.exact else preview(spec))
    return 0


async def _refresh(hub: Any) -> None:
    """Un tour de suivi sur chaque machine (statuts à jour)."""
    machines = [m.id for m in hub.service.inventory.machines]
    await asyncio.gather(*(hub.service.monitor.poll(m) for m in machines))


def _print_runs(runs: list[Run]) -> None:
    if not runs:
        print("aucun run")
        return
    rows = [
        (
            r.id,
            str(r.status),
            format_duration(elapsed_seconds(r)),
            r.project_id,
            f"{r.machine_id} · {r.image_id} · {r.venv_id} · {r.config}",
            "" if r.exit_code is None else str(r.exit_code),
        )
        for r in runs
    ]
    header = ("ID", "STATUT", "DURÉE", "PROJET", "MACHINE · IMAGE · VENV · CONFIG", "CODE")
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows, strict=False)]
    for row in (header, *rows):
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=False)).rstrip())


async def _launch(args: argparse.Namespace) -> int:
    async with _hub(args) as hub:
        run = await hub.launch(
            project=args.project,
            machine=args.machine,
            image=args.image,
            venv=args.venv,
            config=args.config,
            overrides=args.overrides,
            cuda_visible_devices=args.gpus,
            params=_params(args),
        )
        print(f"{run.id}  {run.status}  {run.container_name}", file=sys.stderr)
        if run.error:
            print(f"erreur : {run.error}", file=sys.stderr)
            return 1
        if args.follow:
            await _follow(hub, run.id)
            await _refresh(hub)
            run = await hub.get(run.id)
            print(f"\n{run.id}  {run.status}  code={run.exit_code}", file=sys.stderr)
        else:
            print(run.id)
    return 0


async def _follow(hub: Any, run_id: str) -> None:
    out = sys.stdout.buffer
    async for chunk in hub.stream_logs(run_id):
        out.write(chunk)
        out.flush()


async def _ps(args: argparse.Namespace) -> int:
    async with _hub(args) as hub:
        if not args.no_refresh:
            await _refresh(hub)
        status = RunStatus(args.status) if args.status else None
        runs = await hub.runs(project=args.project, status=status)
        if not args.all and status is None:
            runs = [r for r in runs if r.status.is_active]
        _print_runs(list(reversed(runs)))
    return 0


async def _logs(args: argparse.Namespace) -> int:
    async with _hub(args) as hub:
        if args.follow:
            await _follow(hub, args.run_id)
            return 0
        raw = await hub.logs(args.run_id, args.tail)
        if args.clean:
            print(readable_logs(raw, tail=args.tail, max_chars=10**9))
        else:
            sys.stdout.buffer.write(raw)
    return 0


async def _action(args: argparse.Namespace) -> int:
    async with _hub(args) as hub:
        await _refresh(hub)
        if args.cmd == "rm":
            await hub.delete(args.run_id)
            print(f"{args.run_id} supprimé")
            return 0
        fn = {"stop": hub.stop, "pause": hub.pause, "resume": hub.resume}[args.cmd]
        run = await fn(args.run_id)
        print(f"{run.id}  {run.status}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .api.app import serve

    if args.inventory:
        os.environ["TRAINHUB_INVENTORY"] = args.inventory
    if args.executor:
        os.environ["TRAINHUB_EXECUTOR"] = args.executor
    if args.data_dir:
        os.environ["TRAINHUB_DATA_DIR"] = args.data_dir
    serve(host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp.stdio import run_stdio_proxy

    asyncio.run(run_stdio_proxy(args.url, token=args.token))
    return 0


def _params(args: argparse.Namespace) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in args.param:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise SystemExit(f"--param attend NOM=VALEUR : {item!r}")
        out[name] = value
    return out


def _add_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("-p", "--project", required=True)
    p.add_argument("-m", "--machine", required=True)
    p.add_argument("-i", "--image", required=True)
    p.add_argument("-e", "--venv", required=True)
    p.add_argument("-c", "--config", required=True)
    p.add_argument("--gpus", help="CUDA_VISIBLE_DEVICES (ex. 0,1)")
    p.add_argument(
        "-P",
        "--param",
        action="append",
        default=[],
        metavar="NOM=VALEUR",
        help="Paramètre libre de la commande ({nom}), répétable",
    )
    p.add_argument("overrides", nargs="*", help="Overrides Hydra (ex. trainer.max_epochs=5)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trainhub", description=__doc__.split("\n")[0])
    parser.add_argument("--inventory", help="Chemin de inventory.yaml (TRAINHUB_INVENTORY)")
    parser.add_argument(
        "--executor", choices=["fake", "local", "ssh"], help="Exécuteur (TRAINHUB_EXECUTOR)"
    )
    parser.add_argument("--data-dir", help="Répertoire de la base SQLite (TRAINHUB_DATA_DIR)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preview", help="Affiche la commande complète sans rien exécuter")
    _add_selection(p)
    p.add_argument("--command", help="Surcharge de la commande générique du projet")
    p.add_argument("--run-id", help="Identifiant du run (défaut : aléatoire)")
    p.add_argument("--exact", action="store_true", help="Une ligne, copiable telle quelle")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("launch", help="Lance un run")
    _add_selection(p)
    p.add_argument("-f", "--follow", action="store_true", help="Suit les logs jusqu'à la fin")
    p.set_defaults(func=lambda a: asyncio.run(_launch(a)))

    p = sub.add_parser("ps", help="Liste les runs (actifs par défaut)")
    p.add_argument("-a", "--all", action="store_true", help="Inclut les runs terminés")
    p.add_argument("--project")
    p.add_argument("--status", choices=[s.value for s in RunStatus])
    p.add_argument("--no-refresh", action="store_true", help="N'interroge pas les machines")
    p.set_defaults(func=lambda a: asyncio.run(_ps(a)))

    p = sub.add_parser("logs", help="Affiche les logs d'un run")
    p.add_argument("run_id")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--tail", type=int, default=None)
    p.add_argument("--clean", action="store_true", help="Sans ANSI, \\r résolus")
    p.set_defaults(func=lambda a: asyncio.run(_logs(a)))

    for name, help_ in (
        ("stop", "Arrête un run"),
        ("pause", "Met un run en pause"),
        ("resume", "Reprend un run en pause"),
        ("rm", "Supprime un run terminé et son conteneur"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("run_id")
        p.set_defaults(func=lambda a: asyncio.run(_action(a)))

    p = sub.add_parser("serve", help="Démarre le serveur web (API, WebSocket, MCP, front)")
    p.add_argument("--host", default=None, help="TRAINHUB_HOST (défaut 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="TRAINHUB_PORT (défaut 8420)")
    p.add_argument("--reload", action="store_true", help="Rechargement auto (développement)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mcp", help="Serveur MCP stdio relié au serveur TrainHub en cours")
    p.add_argument(
        "--url",
        default=None,
        help="URL du serveur (défaut http://TRAINHUB_HOST:TRAINHUB_PORT)",
    )
    p.add_argument("--token", default=None, help="Jeton (défaut TRAINHUB_TOKEN)")
    p.set_defaults(func=cmd_mcp)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from .logging_setup import setup_logging

    setup_logging(os.environ.get("TRAINHUB_LOG_LEVEL", "WARNING"))
    parser = build_parser()
    args = parser.parse_args(argv)
    from .service import ServiceError

    try:
        code: int = args.func(args)
    except (InventoryError, UnknownResourceError, ServiceError, ValueError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return code


if __name__ == "__main__":
    raise SystemExit(main())
