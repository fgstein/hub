"""Zip the repository for distribution, without anything git ignores.

The file list comes from git (tracked files plus untracked ones that are not ignored), so
.gitignore decides what stays out: .venv, caches, node_modules, .trainhub (database, logs),
dev/ssh (keys)… .claude/ is skipped as well. Uncommitted changes are included.

The built front end (web/dist, git-ignored) is added as src/trainhub/static, inside the
package: `pip install .` or `uv sync` from the archive then serves the interface without Node.
Build it first (`make zip` runs `make build-web`).

The archive is named trainhubX.Y.Z.zip, X.Y.Z being the version in pyproject.toml, and its
entries live under a top-level `trainhub/` folder, so unzipping it gives one directory.

Usage:
    uv run python scripts/make_zip.py [--out ../trainhubX.Y.Z.zip]
"""

from __future__ import annotations

import argparse
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "trainhub"

# Paths relative to the repository root; a directory excludes everything in it.
EXCLUDED_PATHS = [Path(".claude")]
# Built front end, and where it goes in the archive (found there by api.app.find_static_dir).
WEB_DIST = ROOT / "web" / "dist"
STATIC_IN_PACKAGE = Path("src/trainhub/static")


def read_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def is_excluded(rel: Path) -> bool:
    return any(rel == ex or ex in rel.parents for ex in EXCLUDED_PATHS)


def repo_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode()
    # --cached still lists files deleted from the working tree but not yet committed.
    rels = {Path(p) for p in out.split("\0") if p}
    return sorted(r for r in rels if (ROOT / r).is_file() and not is_excluded(r))


def main() -> None:
    parser = argparse.ArgumentParser(description="Zip the repository for distribution.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"archive to write (default: {NAME}X.Y.Z.zip next to the repository)",
    )
    args = parser.parse_args()
    if not (WEB_DIST / "index.html").is_file():
        raise SystemExit("web/dist is missing: build the front end first (make build-web)")
    out = (args.out or ROOT.parent / f"{NAME}{read_version()}.zip").resolve()

    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in repo_files():
            if ROOT / rel == out:
                continue
            zf.write(ROOT / rel, Path(NAME) / rel)
            count += 1
        for path in sorted(WEB_DIST.rglob("*")):
            if path.is_file():
                zf.write(path, Path(NAME) / STATIC_IN_PACKAGE / path.relative_to(WEB_DIST))
                count += 1

    print(f"Wrote {count} files to {out}")


if __name__ == "__main__":
    main()
