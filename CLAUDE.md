# TrainHub — notes for Claude

## `web/dist` is committed: rebuild it with every front-end change

The built front end (`web/dist`) is tracked in git on purpose, so that `git clone` +
`pip install .` (or `uv sync`) serves the interface without Node or npm. It is a build output, so
it goes stale as soon as `web/` changes without a rebuild.

- After **any** change under `web/` (sources, `package.json`, `package-lock.json`, Vite config,
  styles, assets), run `make build-web` and stage `web/dist` in the **same commit** as the change.
  Use `git add -A web/dist`: file names are content-hashed, so old assets are deleted and new
  ones appear.
- Never edit files in `web/dist` by hand, and never resolve a merge conflict in it by picking a
  side: resolve `web/src`, then run `make build-web` again.
- Before a commit that touches `web/`, check that `git status -- web/dist` lists the rebuilt files.
  A front-end change with no change in `web/dist` means the rebuild was forgotten.
- CI (`.github/workflows/ci.yml`, web job) rebuilds and fails if `web/dist` differs from what is
  committed. The build is reproducible with Node 22 (same as CI); with another Node version the
  output may differ.
- `pyproject.toml` ships `web/dist` inside the wheel as `trainhub/static`
  (`[tool.hatch.build.targets.wheel.force-include]`), so the wheel build fails if it is missing.
  `api.app.find_static_dir` looks in `TRAINHUB_STATIC_DIR`, then `trainhub/static`, then
  `web/dist` (editable installs).
- The Docker image rebuilds the front end itself (`.dockerignore` excludes `web/dist`).

## Bump the minor version with every requested change

Each change the user asks for bumps the **minor** version (`0.4.2` → `0.5.0`), in the same
merge: `pyproject.toml`, `src/trainhub/__init__.py`, `web/package.json`, the two top
`"version"` fields of `web/package-lock.json` (not the dependencies that happen to share the
number), then `uv lock`. See the README (Quick start).

## Keep `inventory.example.yaml` up to date

`inventory.example.yaml` is the reference users copy to write their inventory. Any change to the
inventory's shape (`core/models.py`: a new section, a new option, a renamed or removed one, a new
default worth knowing) updates it **in the same commit**: optional sections and options appear
commented, in the style of the others, with a one-line comment saying what they do. The README's
section and `dev/inventory.dev.yaml` follow too when they show the setting.

`tests/test_inventory.py` (`test_example_inventory_shows_every_setting`) fails when a field of an
inventory model is missing from the example; a new model of the inventory goes in its list of
sections, and a setting that is not for users goes in `NOT_IN_EXAMPLE`.
