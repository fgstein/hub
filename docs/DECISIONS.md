# Implementation choices and deviations from the specification

Specification: issue #1 (“TrainHub — Hydra training launcher”). This document lists, step by
step, what was done, the choices made alone where the spec left room, and the deviations.
Items marked **[to confirm]** deserve a confirmation.

## Open points of the spec (section 12)

| Point | Decision applied |
|---|---|
| GPU choice | Optional `CUDA_VISIBLE_DEVICES` field per run (`-e CUDA_VISIBLE_DEVICES=…`), in the draft (“GPU”), the API (`cuda_visible_devices`), MCP and the CLI (`--gpus`). Validated value (`0,1`, `all`, `none`). |
| Hydra overrides | No field in the draft (spec default). The core, the API, MCP (`overrides`) and the CLI (positional arguments) accept them, each one escaped. |
| Up-to-date code | Mounted as is (already present on the machine): nothing is done before launch. |
| Multi-user | Single user, optional token (`TRAINHUB_TOKEN`). |
| Cleanup | No automatic deletion; manual deletion. |
| Color of `stopped` | **Grey** (static dot with ■), to tell a deliberate stop from a failure. |
| Notifications | No browser notifications. |

## Step 1 — foundation and core

- `core/commands.py`: the `docker_*` functions return the **remote** argv; `transport()`
  wraps it in `ssh … -- '<remote command>'` (a single argument, `shlex.join`), except for a
  `localhost` machine (without `user` or `port`). The spec's `build_*` functions return the
  full argv.
- Extra labels on containers: `trainhub.machine`, `trainhub.image`, `trainhub.venv`,
  `trainhub.config` (in addition to `trainhub`, `trainhub.run_id`, `trainhub.project`). They
  allow importing an unknown container with all its parameters and telling apart “machines”
  that share the same Docker (`local` mode).
- `-w <workdir>` is passed to `docker run` in addition to the `cd` in the script (as in the example).
- `preview()` returns the spec's readable multi-line form (a single escaping level);
  `preview_exact()` returns the exact line ready to copy (used by the “copy” button).
- Placeholders: `{name}` not preceded by `$`, to let `${VAR}` and Hydra interpolations
  `${oc.env:X}` through. An unknown placeholder is a validation error.
- Escaping tests: actually running both shell levels (`sh -c` for the remote shell, `bash -c`
  for the container) with spaces, `'`, `"`, `$`, `;`, `` ` ``, `$(…)`, `*`, `\`, unicode and
  the spec's Hydra overrides.
- Configuration through environment variables: `config.py`. Default of `TRAINHUB_DATA_DIR`:
  `./.trainhub` (the spec sets no default; `/var/lib/trainhub` in the Docker image).

## Step 2 — executors and service

- `Executor` interface: `exec(machine, remote_argv, timeout)`, `stream(machine, argv)` and
  `full_argv()` (audit). Timeouts: 10 s per action, 5 s for monitoring, 600 s for `docker run`
  (which may pull the image), `stop_timeout + 10 s` for `docker stop`.
- `ssh`: `BatchMode=yes` and `ConnectTimeout=5` options added (never an interactive prompt);
  exit code 255 or timeout = machine unreachable.
- `fake` executor: it interprets the docker argv actually built (which also tests command
  building). An image whose name contains `missing` simulates a missing image.
- A stop that cannot happen (machine unreachable) is dropped cleanly (`stop_requested` reset
  to false): when the machine comes back the run becomes `running` again and can be stopped again.
- `stopped` vs `failed`: an internal `stop_requested` field (persisted) indicates that a stop
  came from TrainHub. Internal `paused_at` field to compute `paused_total_s`.
- A run in the database whose container disappeared becomes `failed` (“container not found…”).
- **Relaunch** creates a run with the same parameters and the project's **current command**
  (not the old resolved command). **[to confirm]**
- **Restart** (`docker start`) resets `started_at`, the paused duration and the exit code; the
  container logs (which contain both executions) are reloaded.
- Log hub: when the stream restarts (restart, machine back), a `reset` message clears the
  terminals before resending the content (avoids duplicates).
- The `unreachable` status only applies to active runs; a `launching` run stays handled by its
  launch (which fails with an explicit message if the machine is unreachable).
- `dev/fake-train` test image: Hydra 1.3.2 + tqdm, two venvs, `baseline`, `long`, `broken`
  (`fail_at_epoch: 2`) configs, SIGTERM handling (code 143).

## Step 3 — API and WebSocket

- Routes as in the spec. Additions: `GET /api/runs?status=active|finished` (aliases),
  `refresh=true` on configs, `GET /docs` (Swagger) and `/openapi.json`.
- Error codes: 404 (unknown), 409 (action not allowed for this status, action already in
  progress, machine unreachable), 422 (validation). Body `{"detail": "<readable message>"}`.
- WebSocket: 8-byte binary header = run id in ASCII padded with spaces. Extra JSON messages:
  `logs.reset`, `logs.end`, `ui_state.updated`, `pong`, `error`. The client ignores events with
  a sequence ≤ the last snapshot's; on reconnection the server automatically resends a full
  snapshot.
- TypeScript types generated by `openapi-typescript` (`make openapi`); WebSocket messages,
  absent from the OpenAPI, are typed by hand (`web/src/lib/types.ts`).

## Steps 4-5 — front end

- Theme: OpenCode color/typography tokens (dark by default, light, or “system” which follows
  `prefers-color-scheme`; toggled by the top-right button, remembered locally).
- The theme and the components were rewritten with Tailwind v4 + Kobalte (no copy of the
  OpenCode components, which depend on their own system). Only the tokens are reused (see
  `THIRD_PARTY_NOTICES`).
- Front-end linter: **Biome** (lint + format) rather than ESLint (the spec allows both).
- Shortcuts: `Ctrl+Alt+←/→` (tabs), `N` (new draft), `Enter` (launch, outside an open list),
  `Escape` (cancel the draft, exit fullscreen).
- Drafts belong to each browser (not persisted); the last selection per project is persisted
  on the server and used as the initial value.
- The command preview uses the selection of the first complete draft, else the project's last
  selection.
- The drafts' “+” button is always visible under the active runs; the empty state shows a
  prominent “New run” button.
- An expanded terminal stays open when its run moves to “Finished” (state remembered per run).
- A run that never started (launch failure) shows “—” as its duration.
- Main bundle ≈ 72 kB gzip; xterm and its addons are loaded when a terminal is first opened
  (separate chunk). Checked in CI (< 300 kB).

## Step 6 — MCP

- **`mcp` SDK pinned to `<2`**: version 2 renamed `FastMCP` to `MCPServer` with API changes;
  the spec asks for FastMCP. Migration to plan later. **[to confirm]**
- Optional parameters added to `launch_run`/`preview_command`: `overrides`,
  `cuda_visible_devices`. `launch_run` and `stop_run` wait up to 15 s for the result.
- `get_logs`: 200 lines by default, `grep` = case-insensitive regex (plain text if the regex is
  invalid), output truncated to 12,000 characters with a note.
- The SDK's DNS rebinding protection is disabled: TrainHub listens on 127.0.0.1 by default and
  network security goes through the token.

## Step 7 — SSH and deployment

- Simulated machines: `ubuntu:24.04` base + `openssh-server`, static Docker client copied from
  `docker:27-cli`, host Docker socket mounted. User `alice`, configs discoverable in
  `/home/alice/seg/configs`. Ports 2201/2202 on 127.0.0.1.
- Dockerfile: `node:22-slim` (front-end build), `python:3.12-slim` (wheel then final image with
  `openssh-client` and `tini`). User `th` (UID 1000, configurable), writable `~/.ssh` for the
  ControlMaster sockets, `HEALTHCHECK` on `/api/health`.
- Server logs in JSON (one line per event), uvicorn included; every executed command is logged
  with its duration and exit code (`docker ps` at DEBUG level to avoid noise, the rest at INFO).
- Project license not chosen (up to the repository owner).

## Checks performed in the development environment

- `pytest`: core, service (fake), API/WebSocket, MCP (in-memory, HTTP, stdio), SSH executor
  (fake binary), real local Docker integration (`trainhub-fake-train` image) and real full SSH
  path to the simulated machines (including a machine going down then coming back).
- `vitest` (formatting, stores) and Playwright (draft → launch → terminal → fullscreen →
  pause/resume → stop, failure on an unreachable machine, command, tabs, 900 px width).
- Docker image built and started in fake mode (non-root user, front end served). In the
  sandbox, network access for the builds required local settings (proxy, mirror) not included
  in the repository.

## Change — choosing the config in the tree (request of October 11)

Request: the config is the name of a Hydra YAML file; the root of the files is given per
project; the user picks by clicking in the full (recursive) tree; each config shows how many
runs have already been launched on it.

- Inventory: `configs.root` (short form of `configs.discover.path`), `pattern` (default
  `*.yaml`), `exclude` (globs of relative paths, e.g. `hydra/*`), `max_depth` (default 10).
  `root: ~/x` is relative to the SSH session's home.
- Discovery: `find -L <root> -maxdepth N -type f -name <pattern>` on the chosen machine
  (instead of `ls`), 60 s cache, ↻ button. Hidden components ignored. An unreadable
  subdirectory does not prevent showing the rest.
- **Config identifier = relative path without extension** (`experiments/unet_large`), and not
  the basename alone: two files with the same name in two directories stay distinct, and it is
  the value Hydra accepts for `--config-name` (config_path = root). The basename remains
  available through the new `{config_name}` placeholder (e.g. `--config-path …
  --config-name {config_name}`). **[to confirm]**
- Counters: computed live on the front end from the run state (all statuses, and running) per
  file and cumulated per directory; on the server, `run_counts` in
  `GET /api/projects/{id}/configs` and in the `list_configs` MCP tool. A deleted run no longer
  counts.
- Picker: popover with search (filter on the path, parent directories kept), collapsible tree
  (everything expanded by default), keyboard navigation (↑/↓, →/← to expand/collapse or go up
  to the parent directory, Enter), root shown at the top, legend at the bottom.

## Change — visible command, kept container, saved logs, relaunch as a draft (October 11)

Requests: see the command used; do not close the container at the end and be able to attach
to it; save the logs of every run and keep showing them after the container is closed;
“Relaunch” opens a draft; Hydra overrides field per run. The `docker run` options stay in the
YAML (no settings menu, owner's decision).

- **Why logs “disappeared”**: (1) in `fake` mode (`make dev`, `--reload`) containers live in
  memory and disappear on every backend reload; (2) a terminal collapsed for more than 10 s
  then reopened no longer received the history: the unsubscription was registered by an
  `onCleanup` placed after an `await`, never run (fixed); (3) the client only received
  `--tail 2000`; (4) nothing survived a `docker rm`.
- **Kept container** (`docker.keep_alive`, default `true`): the command runs under `bash` in
  the background (`CMD & pid=$!`), `SIGTERM` is forwarded to it; at the end the code is written
  to `/tmp/.trainhub-exit` then the container waits (`sleep` in a loop). The end is detected by
  an `unhealthy` healthcheck (visible in `docker ps`, so no monitoring cost); the exit code is
  read from `State.Health.Log` by `docker inspect`. Limitation: a compound command (`a && b`)
  receives `SIGTERM` on the subshell, not on each process. **[to confirm]**
- `container_alive` field on runs (automatic SQLite migration). New `close` action
  (`POST /api/runs/{id}/close` API, `close_container` MCP tool); “Restart” uses
  `docker restart` if the container is still started; “Delete” runs `docker rm -f`.
- **Shell**: `/ws/shell/{run_id}` WebSocket attached to a local PTY that runs
  `ssh -tt … docker exec -it <container> sh -c '<cd workdir; . venv; exec bash -i>'`. The
  session closes with the tab or the page. Fake mode: simulated shell (no command executed).
  Available for a running run or a kept container, not while paused.
- **Logs**: every active run is followed permanently (not only the displayed ones) and written
  unbuffered to `$TRAINHUB_DATA_DIR/logs/<run>.log`; full `docker logs` (no more `--tail`) on
  every (re)connection, the session rewrites the file through `<run>.log.part`, which replaces
  it as soon as it has caught up with the old one (see `_LogFile`). `GET /api/runs/{id}/logs`
  and `get_logs` read the file first. Files are kept when a run is deleted.
- **Relaunch** (interface) opens a prefilled draft (machine, image, venv, config, GPU,
  overrides), highlighted; nothing is launched before “Launch”. The `relaunch_run` MCP tool
  still launches directly.
- **Hydra overrides**: text field of the draft, split as a shell would (quotes, `\`), red
  border if a quote is not closed.
- Run panel: “command / full” bar (full = multi-line, a single escaping level; the button
  copies the exact line), “logs / shell” tabs.
- **Running run as a template**: “New run from this one…” entry in the menu of active runs
  (same prefilled draft as “Relaunch…”, the original run keeps going).

## Change — `system` venv and free parameters in the command (October 11)

- **`system` venv**: always offered (last) for every project and every image; no
  `source …/activate` (the built-in shell does not activate anything either). A venv declared
  with the id `system` in the inventory takes precedence over the implicit venv.
- **Free parameters**: any `{name}` placeholder other than `config`, `config_name`, `run_id`,
  `machine`, `project` is no longer an error but a run parameter (`ProjectView.params`). Its
  value is split as a shell would (`shlex.split`) then each word is escaped (`shlex.quote`):
  `{options}` can carry several arguments, `"a b"` makes a single one, an empty value inserts
  nothing. An unclosed quote is rejected (422, field in red).
  **[to confirm]**: splitting rather than a single argument per parameter.
- Persistence: `Run.params` (SQLite column added by migration), last values per project in
  `last_selection.params` (draft defaults), carried over by “Relaunch…” / “New run from this
  one…”; the MCP relaunch only keeps the parameters still present in the current command.
  Parameter unknown to the command: explicit error. API/MCP: `params` (name → value object);
  CLI: `-P name=value`.
- Draft: second line with one field per parameter, in the order of the command; parameters are
  colored orange in the command editor.
- Fix: a draft dropdown closed by a click on another field no longer takes the focus back.
- **“Hydra overrides” field removed from the draft**: the `{name}` parameters replace it (add
  `{options}` to the project command). Overrides remain in the API, MCP and the CLI; the field
  only reappears in a draft coming from a run that had some (otherwise they would be lost
  without being seen).

## Change — English translation (October 11)

Code comments and docstrings, error messages (API, MCP, CLI), the web interface, the dev
inventories and the documentation were translated from French to English. Durations now use
`d` for days (`2d 04h`) and dates are formatted with the `en-GB` locale. Error messages already
stored in an existing SQLite database (e.g. `error` of old runs) are not rewritten.
