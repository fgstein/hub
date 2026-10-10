"""CLI minimale : ``trainhub launch|ps|logs|stop|preview|serve|mcp``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from .core.commands import preview, preview_exact
from .core.ids import new_run_id
from .core.inventory import InventoryError, load_inventory
from .core.models import UnknownResourceError
from .core.selection import make_spec


def _inventory_path(args: argparse.Namespace) -> str:
    return str(args.inventory or os.environ.get("TRAINHUB_INVENTORY", "inventory.yaml"))


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
    )
    print(preview_exact(spec) if args.exact else preview(spec))
    return 0


def _add_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("-p", "--project", required=True)
    p.add_argument("-m", "--machine", required=True)
    p.add_argument("-i", "--image", required=True)
    p.add_argument("-e", "--venv", required=True)
    p.add_argument("-c", "--config", required=True)
    p.add_argument("--gpus", help="CUDA_VISIBLE_DEVICES (ex. 0,1)")
    p.add_argument("--command", help="Surcharge de la commande générique du projet")
    p.add_argument("overrides", nargs="*", help="Overrides Hydra (ex. trainer.max_epochs=5)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trainhub", description=__doc__)
    parser.add_argument("--inventory", help="Chemin de inventory.yaml (TRAINHUB_INVENTORY)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preview", help="Affiche la commande complète sans rien exécuter")
    _add_selection(p)
    p.add_argument("--run-id", help="Identifiant du run (défaut : aléatoire)")
    p.add_argument("--exact", action="store_true", help="Une ligne, copiable telle quelle")
    p.set_defaults(func=cmd_preview)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code: int = args.func(args)
    except (InventoryError, UnknownResourceError, ValueError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return code


if __name__ == "__main__":
    raise SystemExit(main())
