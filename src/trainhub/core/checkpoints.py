"""Checkpoint sources: tree layout, listing and deletion commands (pure functions, no I/O).

A source is a directory of runs on a machine (``root``), launched by TrainHub or not. Its
``layout`` describes where the checkpoints are, e.g. ``{name}/checkpoints/{group}/*.ckpt``:

- ``{name}``: first level, a run (one directory per run);
- ``{group}``: optional grouping level (``topk``, ``last``…);
- other directories: literal names (globs allowed);
- last component: the checkpoint file pattern.

Listing is a single ``find`` on the machine (GNU ``-printf``: type, date, size, path).
A file under a run that does not follow the layout is kept, as an “other” file of the run.
"""

from __future__ import annotations

import fnmatch
import posixpath
from collections.abc import Container, Iterable, Sequence
from dataclasses import dataclass

NAME = "{name}"
GROUP = "{group}"
#: ``$0`` of the deletion script (recognized by the fake executor).
RM_SCRIPT_NAME = "trainhub-rm"


@dataclass(frozen=True)
class Layout:
    """Parsed ``layout``: directory components after ``{name}``, then the file pattern."""

    dirs: tuple[str, ...]
    """Components between ``{name}`` and the file: ``{group}`` or a literal glob."""
    file: str
    """File name pattern (e.g. ``*.ckpt``)."""

    @property
    def depth(self) -> int:
        """Depth of a checkpoint under the root (``{name}`` = 1)."""
        return len(self.dirs) + 2

    @property
    def group_index(self) -> int | None:
        """Index of ``{group}`` in a path relative to the root."""
        return self.dirs.index(GROUP) + 1 if GROUP in self.dirs else None


def parse_layout(layout: str) -> Layout:
    """Validate a layout; raise ``ValueError`` with a readable message."""
    parts = layout.strip().split("/")
    if len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
        raise ValueError("layout: relative path expected, e.g. {name}/checkpoints/{group}/*.ckpt")
    if parts[0] != NAME:
        raise ValueError("layout: the first component must be {name} (one directory per run)")
    *dirs, file = parts[1:]
    if dirs.count(GROUP) > 1:
        raise ValueError("layout: {group} may appear only once")
    for p in [*dirs, file]:
        if p != GROUP and ("{" in p or "}" in p):
            raise ValueError(f"layout: unknown placeholder in {p!r} (allowed: {{name}}, {{group}})")
        if p.startswith(".") or "**" in p:
            raise ValueError(f"layout: invalid component {p!r}")
    if file == GROUP:
        raise ValueError("layout: the last component is the file pattern, not {group}")
    return Layout(dirs=tuple(dirs), file=file)


# -- listing --------------------------------------------------------------------------


def list_checkpoints(root: str, layout: Layout) -> list[str]:
    """Remote argv listing the directories and checkpoint files under ``root``.

    One line per entry: ``d<TAB>mtime<TAB>0<TAB>path`` or ``f<TAB>mtime<TAB>size<TAB>path``
    (path relative to the root). Hidden components are pruned.
    """
    return [
        "find",
        "-L",
        root,
        "-mindepth",
        "1",
        "-maxdepth",
        str(layout.depth),
        "(",
        "-name",
        ".*",
        "-prune",
        ")",
        "-o",
        "(",
        "-type",
        "d",
        "-printf",
        r"d\t%T@\t0\t%P\n",
        ")",
        "-o",
        "(",
        "-type",
        "f",
        "-name",
        layout.file,
        "-printf",
        r"f\t%T@\t%s\t%P\n",
        ")",
    ]


@dataclass(frozen=True)
class Entry:
    """A directory or a checkpoint file found under the root."""

    kind: str  # "d" | "f"
    path: str  # relative to the root
    mtime: float
    size: int = 0

    @property
    def parts(self) -> list[str]:
        return self.path.split("/")


def parse_listing(output: str) -> list[Entry]:
    """``find`` output → entries (malformed lines and hidden paths ignored)."""
    out: list[Entry] = []
    for line in output.splitlines():
        fields = line.rstrip("\r").split("\t", 3)
        if len(fields) != 4 or fields[0] not in ("d", "f"):
            continue
        kind, mtime, size, path = fields
        if not valid_relpath(path, min_parts=1):
            continue
        try:
            out.append(Entry(kind=kind, path=path, mtime=float(mtime), size=int(size)))
        except ValueError:
            continue
    return out


def valid_relpath(path: str, *, min_parts: int = 2) -> bool:
    """Relative path without ``.``/``..``/empty/hidden components nor control characters."""
    if not path or path.startswith("/") or any(ord(c) < 32 for c in path):
        return False
    parts = path.split("/")
    return len(parts) >= min_parts and not any(p in ("", ".", "..") or p[0] == "." for p in parts)


@dataclass(frozen=True)
class Placement:
    """Where a checkpoint file sits in the tree of its source."""

    run: str
    group: str | None
    """Group (``""`` if the layout has no ``{group}``); ``None``: file outside the layout."""


def place(path: str, layout: Layout) -> Placement | None:
    """Run and group of a checkpoint file; ``None`` if it is not under a run directory."""
    parts = path.split("/")
    if len(parts) < 2 or not fnmatch.fnmatchcase(parts[-1], layout.file):
        return None
    if len(parts) != layout.depth:
        return Placement(run=parts[0], group=None)
    for comp, pat in zip(parts[1:-1], layout.dirs, strict=True):
        if pat != GROUP and not fnmatch.fnmatchcase(comp, pat):
            return Placement(run=parts[0], group=None)
    gi = layout.group_index
    return Placement(run=parts[0], group=parts[gi] if gi is not None else "")


def group_dir(run: str, group: str, layout: Layout) -> str | None:
    """Relative directory of a group, if the layout has only literal names before it."""
    gi = layout.group_index
    if gi is None:
        return None
    prefix = layout.dirs[: gi - 1]
    if any(any(c in p for c in "*?[") for p in prefix):
        return None
    return "/".join([run, *prefix, group])


def is_group_dir(path: str, layout: Layout) -> bool:
    """``True`` if ``path`` (relative) is a ``{group}`` directory of the layout."""
    gi = layout.group_index
    parts = path.split("/")
    if gi is None or len(parts) != gi + 1:
        return False
    return all(
        fnmatch.fnmatchcase(c, p) for c, p in zip(parts[1:-1], layout.dirs[: gi - 1], strict=True)
    )


# -- deletion -------------------------------------------------------------------------

#: Arguments: root, files to delete, "" then directories to remove if empty. Each path
#: is resolved (``realpath``) and must stay under the resolved root (no escape through a
#: symbolic link). One line per path: ``ok``, ``gone``, ``err<TAB>message`` or ``rmdir``.
_RM_SCRIPT = r"""
root=$(realpath -e -- "$1" 2>/dev/null) || { printf 'root\t%s\n' "$1"; exit 2; }
shift
mode=files
inside() { case "$1/" in "$root"/?*) return 0;; *) return 1;; esac; }
for rel do
  if [ -z "$rel" ]; then mode=dirs; continue; fi
  p="$root/$rel"
  if [ "$mode" = dirs ]; then
    d=$(realpath -e -- "$p" 2>/dev/null) || continue
    inside "$d" && [ "$d" != "$root" ] && rmdir -- "$d" 2>/dev/null && printf 'rmdir\t%s\n' "$rel"
    continue
  fi
  if [ ! -e "$p" ] && [ ! -L "$p" ]; then printf 'gone\t%s\n' "$rel"; continue; fi
  d=$(realpath -e -- "$(dirname -- "$p")" 2>/dev/null)
  if [ -z "$d" ] || ! inside "$d"; then printf 'err\t%s\toutside the root\n' "$rel"; continue; fi
  if [ -d "$p" ]; then printf 'err\t%s\tis a directory\n' "$rel"; continue; fi
  if msg=$(rm -f -- "$p" 2>&1); then printf 'ok\t%s\n' "$rel"
  else printf 'err\t%s\t%s\n' "$rel" "$(printf %s "$msg" | tr '\n\t' '  ')"; fi
done
""".strip()


def delete_checkpoints(root: str, files: Sequence[str], dirs: Iterable[str] = ()) -> list[str]:
    """Remote argv deleting ``files`` then removing ``dirs`` if empty (paths relative)."""
    return ["sh", "-c", _RM_SCRIPT, RM_SCRIPT_NAME, root, *files, "", *dirs]


@dataclass(frozen=True)
class DeleteOutcome:
    deleted: list[str]
    """Deleted files, and files already missing."""
    failed: dict[str, str]
    removed_dirs: list[str]
    root_missing: bool = False


def parse_delete_output(output: str, files: Sequence[str]) -> DeleteOutcome:
    deleted: list[str] = []
    failed: dict[str, str] = {}
    removed: list[str] = []
    root_missing = False
    for line in output.splitlines():
        fields = line.split("\t", 2)
        tag = fields[0]
        if tag == "root":
            root_missing = True
        elif tag in ("ok", "gone") and len(fields) >= 2:
            deleted.append(fields[1])
        elif tag == "err" and len(fields) == 3:
            failed[fields[1]] = fields[2].strip() or "deletion failed"
        elif tag == "rmdir" and len(fields) >= 2:
            removed.append(fields[1])
    done = set(deleted) | set(failed)
    for f in files:
        if f not in done:
            failed[f] = "root not found" if root_missing else "no result (interrupted?)"
    return DeleteOutcome(deleted, failed, removed, root_missing)


# -- MLflow ---------------------------------------------------------------------------


def path_from_run(value: str, names: Container[str]) -> str | None:
    """Path relative to the root of a path logged to MLflow, from its run directory.

    The path may be seen from the container (other prefix than ``root``): only the
    components are compared, and the last one that is a run of ``names`` wins, e.g.
    ``/workspace/logs/runs/run_a/checkpoints/last.ckpt`` → ``run_a/checkpoints/last.ckpt``.
    """
    parts = [p for p in value.strip().replace("\\", "/").split("/") if p]
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in names:
            return "/".join(parts[i:])
    return None


def run_in_path(value: str, names: Container[str]) -> str | None:
    """Run directory named in a path logged to MLflow (see :func:`path_from_run`)."""
    rel = path_from_run(value, names)
    return rel.split("/", 1)[0] if rel else None


# -- paths in the container -----------------------------------------------------------


def container_path(host_path: str, volumes: Sequence[str]) -> str | None:
    """Path seen in the container for a path of the machine, through the ``-v`` mounts.

    The most specific mount wins; ``None`` if no bind mount covers the path.
    """
    path = posixpath.normpath(host_path)
    best: tuple[int, str] | None = None
    for vol in volumes:
        parts = vol.split(":")
        if len(parts) < 2 or not parts[0].startswith("/") or not parts[1].startswith("/"):
            continue
        host, cont = posixpath.normpath(parts[0]), posixpath.normpath(parts[1])
        if path == host:
            rest = ""
        elif path.startswith(host.rstrip("/") + "/"):
            rest = path[len(host.rstrip("/")) + 1 :]
        else:
            continue
        if best is None or len(host) > best[0]:
            best = (len(host), posixpath.join(cont, rest) if rest else cont)
    return best[1] if best else None
