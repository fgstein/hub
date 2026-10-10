"""Chargement et validation de ``inventory.yaml``.

Les erreurs de validation sont reformulées de façon lisible, avec le fichier, la ligne et le
chemin du champ fautif (ex. ``inventory.yaml:42 projects[0].command : ...``).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import Inventory


class InventoryError(ValueError):
    """Inventaire illisible ou invalide."""

    def __init__(self, source: str, problems: Sequence[str]) -> None:
        self.source = source
        self.problems = list(problems)
        super().__init__(self.__str__())

    def __str__(self) -> str:
        return f"inventaire invalide ({self.source}) :\n" + "\n".join(
            f"  - {p}" for p in self.problems
        )


def _locate(node: yaml.Node | None, loc: Sequence[int | str]) -> int | None:
    """Ligne (1-based) du nœud YAML le plus profond correspondant à ``loc``."""
    line = node.start_mark.line + 1 if node is not None else None
    for part in loc:
        if node is None:
            break
        nxt: yaml.Node | None = None
        if isinstance(node, yaml.MappingNode) and isinstance(part, str):
            for k, v in node.value:
                if isinstance(k, yaml.ScalarNode) and k.value == part:
                    nxt = v
                    line = k.start_mark.line + 1
                    break
        elif (
            isinstance(node, yaml.SequenceNode)
            and isinstance(part, int)
            and 0 <= part < len(node.value)
        ):
            nxt = node.value[part]
            line = nxt.start_mark.line + 1
        node = nxt
    return line


def _fmt_loc(loc: Sequence[int | str]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + part
    return out or "<racine>"


def parse_inventory(text: str, source: str = "inventory.yaml") -> Inventory:
    """Valide un inventaire YAML donné sous forme de texte."""
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{source}:{mark.line + 1}" if mark is not None else source
        raise InventoryError(source, [f"{where} : YAML invalide ({exc})"]) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise InventoryError(source, [f"{source}:1 : la racine doit être un dictionnaire"])
    try:
        return Inventory.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors(include_url=False):
            loc = [p for p in err["loc"] if isinstance(p, int | str)]
            # Les erreurs de model_validator n'ont pas de chemin précis.
            line = _locate(node, loc)
            msg = err["msg"].removeprefix("Value error, ")
            where = f"{source}:{line}" if line else source
            problems.append(f"{where} {_fmt_loc(loc)} : {msg}")
        raise InventoryError(source, problems) from exc


def load_inventory(path: str | Path) -> Inventory:
    """Lit et valide un fichier d'inventaire."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise InventoryError(str(p), [f"lecture impossible : {exc}"]) from exc
    return parse_inventory(text, str(p))
