"""Loading and validation of ``inventory.yaml``.

Validation errors are reworded readably, with the file, the line and the path of the
faulty field (e.g. ``inventory.yaml:42 projects[0].command: ...``).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import Inventory


class InventoryError(ValueError):
    """Unreadable or invalid inventory."""

    def __init__(self, source: str, problems: Sequence[str]) -> None:
        self.source = source
        self.problems = list(problems)
        super().__init__(self.__str__())

    def __str__(self) -> str:
        return f"invalid inventory ({self.source}):\n" + "\n".join(
            f"  - {p}" for p in self.problems
        )


class InventoryNotFoundError(InventoryError):
    """Inventory file missing."""

    def __init__(self, source: str, searched: Sequence[str]) -> None:
        self.searched = list(searched)
        super().__init__(source, [])

    def __str__(self) -> str:
        return (
            f"inventory not found: {self.source}\n"
            "  Pass --inventory or set TRAINHUB_INVENTORY, or create one of: "
            + ", ".join(self.searched)
        )


def _locate(node: yaml.Node | None, loc: Sequence[int | str]) -> int | None:
    """Line (1-based) of the deepest YAML node matching ``loc``."""
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
    return out or "<root>"


def parse_inventory(text: str, source: str = "inventory.yaml") -> Inventory:
    """Validate a YAML inventory given as text."""
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{source}:{mark.line + 1}" if mark is not None else source
        raise InventoryError(source, [f"{where}: invalid YAML ({exc})"]) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise InventoryError(source, [f"{source}:1: the root must be a mapping"])
    try:
        return Inventory.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors(include_url=False):
            loc = [p for p in err["loc"] if isinstance(p, int | str)]
            # model_validator errors have no precise path.
            line = _locate(node, loc)
            msg = err["msg"].removeprefix("Value error, ")
            where = f"{source}:{line}" if line else source
            problems.append(f"{where} {_fmt_loc(loc)}: {msg}")
        raise InventoryError(source, problems) from exc


def load_inventory(path: str | Path) -> Inventory:
    """Read and validate an inventory file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise InventoryError(str(p), [f"cannot read: {exc}"]) from exc
    return parse_inventory(text, str(p))
