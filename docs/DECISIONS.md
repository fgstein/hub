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

## Fix and additions — progress bars, Docker command, sourced files (12/10)

- **tqdm bars missing from the logs.** A `docker run -d -t` creates a **0x0** pseudo-terminal
  (no client attached to size it); tqdm reads that width and prints no bar at all (checked with
  real Docker). The test image forced `ncols=100`, which hid the problem; it no longer does, and
  the Docker integration test checks that the bar is there. Fix: with `tty`, the container script
  starts with `stty cols C rows L 2>/dev/null` and `COLUMNS`/`LINES` are passed; size set per
  project with `docker.tty_size` (default `[100, 40]`).
- **Configurable Docker command.** `docker_command` at the top of the inventory (default
  `docker`), overridable per machine; string (split like a shell) or list. Substitution happens
  at transport time: the core still builds `docker …` (testable, understood by the fake
  executor), then `ssh`/`local` replace `docker` with the machine's command. The preview and the
  audited argv show the real command.
- **Files sourced before the command.** Per project, `env_files` then `source`; each entry is a
  path or `{path, optional}`. `env_files` is sourced between `set -a` and `set +a`, so `AAA=bbb`
  lines are exported like `export AAA=bbb`. Relative paths are resolved against the workdir and
  prefixed with `./` (otherwise `source` would search the PATH); `~/` is expanded. A missing
  mandatory file: `trainhub: file to source not found: …` and exit code 1, before the command
  starts. They are part of the setup steps, so they work with and without `keep_alive`.

## Change — MLflow progress and status, Machines tab (October 16)

Requests: read from MLflow a configurable parameter (maximum epoch) and metric (current epoch)
to show a progress bar; color the status dot from MLflow (green: going well, red: failed,
blue: finished); a tab showing the CPU and GPU load of every machine of the inventory.

- **Link TrainHub run ↔ MLflow run: a tag** (`trainhub.run_id` by default, `run_tag`). To avoid
  any change in the training code, TrainHub passes `MLFLOW_RUN_CONTEXT={"<run_tag>": "<id>"}`
  to the container: MLflow's `SystemEnvironmentContext` adds these tags to every run created
  (checked with MLflow 3.17; also used by Lightning's `MLFlowLogger`). `TRAINHUB_RUN_ID` is
  passed too. A variable of the same name in `docker.env` wins. `inject_env: false` disables
  it. **[to confirm]**
- **REST API called directly** (`httpx`, already a dependency) rather than the `mlflow`
  package (heavy). Search: `runs/search` with `tags.\`<run_tag>\` = '<id>' AND
  attributes.start_time >= <run start − 5 min>` (after a restart, the previous MLflow run is
  ignored), most recent first; once found, `runs/get` by id. Experiments: the listed names
  (`get-by-name`), else all (`experiments/search`, cached 60 s). Checked against a real MLflow
  3.17 server.
- **Settings**: global `mlflow` section, overridable per project (`mlflow: {enabled: false}` to
  disable one project). Defaults: parameter `max_epochs`, metric `epoch` (latest value),
  `epoch_offset: 0`, polling every 10 s. Credentials: `MLFLOW_TRACKING_TOKEN` or
  `MLFLOW_TRACKING_USERNAME`/`PASSWORD` in TrainHub's environment, never in the YAML.
- **Persisted state**: `Run.mlflow` (run id, status, epoch, max epochs, UI link, last error),
  `mlflow` column of the database, published with `run.updated`. Polled while the run is active,
  then up to 10 min after the end until MLflow gives a final status. On an MLflow error the last
  values are kept, with the error (tooltip, “mlflow ⚠”).
- **Dot color** (front end, `lib/mlflow.ts`): running + RUNNING/SCHEDULED → green; FINISHED →
  blue (also succeeded + RUNNING: MLflow lags behind); FAILED/KILLED → red; failed → red.
  Launching, paused, stopping, stopped (grey, deliberate stop although MLflow says KILLED) and
  unreachable keep their colors. Without MLflow information, the colors are unchanged. New
  `--status-blue` token. **[to confirm]**
- **Machines tab** (`/machines`, fixed tab left of the projects): `GET
  /api/machines/{id}/load` runs one `sh -c` on the host (`/proc/loadavg`, two readings of
  `/proc/stat` 0.5 s apart for the usage of each core, `/proc/meminfo`) and the two
  `nvidia-smi` queries, in parallel. An unreachable machine is a `reachable: false` answer, not
  an error. The front end queries each machine separately (a slow machine does not hold the
  others) every 5 s while the tab is open and visible. Load is drawn with a single sequential
  hue (`--load-full`), distinct from the status colors, always with the value written next to
  it. MCP tool `get_machine_load`.
- `fake` mode: simulated MLflow (state of the simulated training: epochs, FINISHED/FAILED/KILLED)
  and simulated `/proc` (64 cores, ~8 busy cores per running command); enabled in
  `dev/inventory.dev.yaml` (except `llm-finetune`).

## Change — series: the same run several times in a row (October 22)

Request: launch several identical runs in a row, with one field drawn at random (e.g. the
seed); sequential; by default the series stops at the first failure, with an option to go on;
all the runs in the same container; integer range; only command parameters can be drawn.

- **One run, one container**: a series is a single TrainHub run whose script runs the command
  `count` times. TrainHub's model is one run = one container (logs, pause, stop, monitoring,
  import), so the repetitions are not separate runs; their state lives in `Run.series`
  (`series` column, JSON): count, `continue_on_failure`, ranges, values of each repetition,
  repetition in progress, exit codes.
- **Script** (`commands._series_body`): the repetitions are unrolled (each one is the resolved
  command with its values, run in the background like a single command, `SIGTERM` forwarded),
  between a start and an end marker. A stop ends the series (also between two repetitions);
  after a failure the next ones are skipped unless `continue_on_failure`. The final code (kept
  container or not) is 0, or the code of the last failure. `TRAINHUB_SERIES_INDEX` is exported
  for each repetition, and `MLFLOW_RUN_CONTEXT` gets `trainhub.series_index`.
- **Progress** read from the markers in the logs (`SeriesTracker`, observer of the log hub): no
  extra command on the machine. Logs read again from the start (new `docker logs` session) only
  move the progress forward; restart resets it.
- **Values drawn at launch** (server side, `SystemRandom`, distinct when the range allows it),
  stored: a relaunch (API/MCP) reproduces them, the interface's “Relaunch…” draft keeps the
  count and ranges and draws again. Default range `0`–`2³¹−1`. At most 100 repetitions.
- **Interface**: `×` field (number of runs) next to “Launch”, die next to each parameter (the
  value field becomes min – max), “continue after a failure” when there are several runs. Run
  row: `2/5` and one cell per repetition (green running, blue succeeded, red failed), tooltip
  with values and codes. The series settings are not remembered in the last selection.
- MLflow: during an active series, the MLflow run is searched again at each poll (the latest
  one carrying the TrainHub run id is the repetition in progress).
- `fake` mode: labels `trainhub.series` / `trainhub.series_continue` on the container; the
  simulation plays each repetition (its own MLflow run) with the markers of the real script.
## Addition — checkpoints and inference, server side (October 29)

Request: infer on checkpoints without specializing TrainHub; a dedicated tab manages the runs
already executed (TrainHub or not, complex trees) and sends a checkpoint to an inference tab.

- **Sources independent of the projects** (`checkpoints:` at the top of the inventory): the tab
  starts from the disk, not from TrainHub runs. No association with TrainHub runs for now
  (a folder name does not necessarily contain a run id); possible later through a declared
  naming template.
- **`layout`**: `{name}` must be the first component (one level per source); `{group}` at most
  once, a single component; no `**`. A file under a run outside the layout stays visible
  (“others”) rather than disappearing silently. The checkpoint name is not parsed.
- **Scan**: one `find -L … -printf` per source (directories and files, type/date/size/path),
  depth limited by the layout, hidden directories pruned. Requires GNU `find`. 5 min cache
  (bigger than configs: a shared disk can be slow), refresh on demand; on error the last good
  listing is kept with the error. Totals (size, count) per source, run and group;
  `checkpoints.updated` event after each scan or deletion.
- **Deletion**: the client sends the exact list of files (deleting a group = its files), so
  only what was seen is deleted. Validated on the server (relative, no `..`/hidden component,
  under a run, file pattern), then on the machine by a `sh` script: `realpath` of the parent
  directory must stay under the resolved root, no directory deleted with `rm`, then `rmdir` of
  the group directories left empty. Direct `rm` with the SSH user (per-file errors). Not exposed
  over MCP (destructive, confirmed by a gesture in the interface). **[to confirm]**
- **`{checkpoint}`**: reserved placeholder, a single escaped word (not split like a free
  parameter, a path may contain spaces). `Selection.checkpoint` = machine path; translated to the
  container path through the project volumes, refused if none covers it. Stored on the run
  (`checkpoint` column, `trainhub.checkpoint` label for import), kept on relaunch if the command
  still takes it. Counts of runs per checkpoint shown in the listing.
- **`extends`**: resolved on the raw YAML before validation; `id`/`label` not inherited,
  `docker` merged key by key, any other field replaced. Unknown parent or cycle: error.
- **Fake mode**: simulated trees per source (`simulation.checkpoints`), deletions applied.

## Addition — Checkpoints tab, front end (October 29)

- **Pinned tab**, outside the project tabs (`/checkpoints/<source>`), shown only if the inventory
  declares sources; not part of the persisted UI state (open tabs remain projects). The last
  chosen source is remembered in the browser. `N` (new draft) is disabled there.
- Every source is loaded when the tab opens (server cache: no rescan within 5 min), so that the
  header shows each total size; the tab shows the sum. `checkpoints.updated` updates the totals
  and reloads a listing changed elsewhere.
- **Two distinct selections**: sending (default mode, click on a line) and deletion (“Delete…”
  mode, red); entering the deletion mode clears the sending selection, so that the two are never
  confused. Checkboxes on groups (tri-state) and checkpoints, not on runs (as requested). Files
  directly under the root can be sent but not deleted (refused by the server).
- **Slider confirmation**: the handle must reach the end of the track (pointer capture); released
  before, it springs back. Keyboard: arrows by 10 %, reaching the end confirms (still a
  deliberate gesture, accessible). After a partial failure, the failed files stay selected and
  the first error is shown.
- **Sending**: one draft per checkpoint in the chosen project, machine = the source's if the
  project allows it (otherwise a message), config = last selection of the project (no
  association with the training run). The draft shows a `{checkpoint}` field (editable, red if
  empty) and the origin “source · run”. `⟲ N` counts the runs live from the store.
- Sizes in decimal units (kB, MB, GB), like file managers.
- E2E: `infer` (extends `seg`) is first in the test inventory so the tab order of the existing
  tests stays `seg → det`. Fake mode: when several projects share a configs root, the simulated
  tree is the one declared for any of them. The “GPU picker” e2e test already failed before this
  change (fixed on October 31, see “Fix — every test green”).

## Addition — mark a run as finished by hand (October 30)

Request: a run stays active (often `unreachable`) when its machine died or its container was
killed outside TrainHub, and could no longer be removed.

- New action `force_finish` (“Mark as finished”, with confirmation), allowed from every active
  status (including `launching`, `stopping`, `unreachable`), even while another action is in
  progress on the run. **Nothing is run on the machine** (it may be down): the run becomes
  `failed`, error “marked as finished by hand (was …): actual outcome unknown”, `finished_at`
  now, no exit code, `container_alive` false. Available in the API
  (`POST /api/runs/<id>/force_finish`), MCP (`force_finish_run`), Python and CLI
  (`trainhub force-finish`).
- Not `stopped`: TrainHub did not stop anything, and the real outcome is unknown.
- **Truth wins**: if the machine answers again with the container really running or paused,
  monitoring shows the run as running again (same rule as a restart outside TrainHub), rather
  than hiding a process that still holds GPUs. A container exited or gone leaves the run finished.
- A stop stuck on the dead machine can no longer bring the run back to an active status when it
  fails: run updates now go through an atomic read-modify-write (`_update` under a lock, event
  published in the same order), with `only_if_active` for the stop failure and for the switch to
  `unreachable`.
- Deleting a finished run still needs the machine (`docker rm`); unchanged.

## Addition — MLflow watch: external runs, stalled runs, failures to be seen (October 31)

Request: bring the principle of mlwatcher (a desktop overlay that watches every run of an MLflow
server) into TrainHub, merged with what TrainHub already shows; mlwatcher stays independent.

- **One search per round** instead of one request per run: per MLflow server, `runs/search`
  of the running runs, then of the runs started since the previous round minus 1 min
  (`start_time >= …`, at most 24 h back), over the experiments concerned. The TrainHub runs take
  their state from these answers (tag `run_tag`, latest start after the run's start − 5 min). A
  run not found there is read on its own (former per-run path) only if it already had an MLflow
  run (it stopped running) or at the first round; otherwise its MLflow run does not exist yet.
  Experiments: `experiments/search` (id → name), cached 60 s. Default period 30 s (was 10 s).
- **External runs** (`ExternalRun`, table `external_runs`, JSON + `hidden`): MLflow runs of the
  experiments a project names (effective `experiments`, first project in the inventory when
  several name one; experiments named by no project are not watched) whose `run_tag` is not a
  known TrainHub run. Start-up: running runs + `FAILED`/`KILLED` since the oldest running one
  (mlwatcher's rule); then running runs and every run started since the previous round. A shown
  run that stops running is read once with `runs/get`; gone or in the MLflow trash: forgotten.
  Not polled once finished. Read only: TrainHub never writes to MLflow for them; deleting one
  only hides it (kept as `hidden` so that it does not come back). Events
  `external_run.updated` / `external_run.deleted`; `external_runs` in the snapshot.
- **Stalled** (`MlflowInfo.stale`, computed by the server at each read): `RUNNING` and no metric
  newer than `stale_after_min` (default 30, per project) — the start counts when there is no
  metric. Orange dot (`Training = "stale"`). New fields `started_at`, `finished_at`,
  `last_activity` (newest metric timestamp).
- **Failures to be seen** (`unseen_failure`, TrainHub and external runs): set when a run
  becomes failed (`Run.is_failure`: status `failed`, or MLflow `FAILED`/`KILLED` while running
  or succeeded — the cases already drawn red), cleared when it no longer is (restart). Such a
  run stays among the active runs until **Seen** (`POST …/seen`, MCP `mark_run_seen`), shared by
  everyone. Not set by “Mark as finished” (the user did it) nor by a stop (`stopped`).
- No notification, no `mlflow.user` filter (asked). The tab shows a red counter of the failures
  not seen; its grey counter now includes the running external runs.
- Fake mode: TrainHub runs log to the first experiment their project names (else one named after
  the project); each named experiment also holds three external runs (training, stalled 45 min,
  failed 20 min before the start). The dev inventory names `segmentation` for that project.
- Test fixed on the way: `test_tracker_with_fake` assumed the container ended before MLflow
  (failed about 2 times out of 3 before this change).

## Change — stalled external runs can be seen or removed (October 31)

Request: a stalled external run (orange) is often a dead one — a process killed without MLflow
knowing stays `RUNNING` forever — and could be neither seen nor removed.

- The trash button of an external run is always there, running or not (it only hides the run
  in TrainHub, as before; the server already allowed it).
- **Seen** on a stalled external run (running, `mlflow.stale`) sets `stale_seen_at`: the run
  moves to the finished runs (“stalled, seen”) and no longer counts in the tab. Cleared as soon
  as it is no longer stalled (a new metric) or ends: it then follows the usual rules (a
  failure is to be seen). Same call as for a failure (`POST /api/external-runs/<id>/seen`, MCP
  `mark_run_seen`). Still nothing written to MLflow.
- TrainHub runs are unchanged: a dead container is seen by the monitoring (`failed`, then
  **Seen**), a stuck training is stopped (*Stop*, or *Mark as finished* if the machine is down).

## Fix — MLflow requests failing about half of the time (October 31)

Report: the MLflow searches often found no run, or said “MLflow unreachable” while the server
was up; running them again a few times worked.

- Every `httpx` error, **timeouts included**, was reported as “MLflow unreachable (…): <error>”,
  and a timeout has an empty message: a slow answer read as an unreachable server. Timeouts are
  now “MLflow did not answer (connect) within N s (server, endpoint)”; other errors get their
  type name when their message is empty.
- 10 s was short for the searches of the checkpoint sources (pages of 1000 runs with all their
  params, metrics and tags, training and inference searches at once, while the polling and other
  clients such as mlwatcher query the same server). They now get 60 s; polling and single reads
  keep 10 s (connect 5 s).
- At most 2 requests in flight per MLflow server (`SERVER_CONCURRENCY`): more only queue up in
  its few workers and all of them run into the timeout.
- A request whose connection is cut (`RemoteProtocolError`, read/write/connect error: a
  kept-alive connection closed by the server as the request went out) is sent once more, never
  `runs/delete`.
- Not reproduced against a real server (none here): deduced from the code and the symptoms.

## Change — inferences from MLflow only; dataset, mapping; show for the training runs (October 31)

Request: linking the TrainHub runs and the MLflow runs of the inferences did not work well
(doubles); simplify: MLflow is the only source of the inferences.

- `CheckpointInference` is an MLflow run (`mlflow` required): no more TrainHub run, project,
  config or status, no merge by `run_tag`, no relaunch button (the user starts from the
  checkpoint). The `⟲ N` count of the TrainHub runs launched with a checkpoint stays.
- `mlflow.inference.dataset_name`, `dataset_path`, `mapping_name`: MLflow keys (params / tags)
  read for each inference. Dataset: a button showing the name (else the last component of the
  path), a click copies the path (disabled without a path). Mapping: a badge, not clickable yet.
  These keys are not repeated among the `show` values unless listed there too.
- `mlflow.show` (training side): values of the latest MLflow run not in the trash, shown on
  the line of each run directory (`CheckpointRun.mlflow_logged`), short key + value.

## Change — no count of TrainHub runs on the checkpoints (October 31)

Request: MLflow only for the inferences, the `⟲ N` included. The count of the TrainHub runs
launched with a checkpoint is gone from the tab (`CheckpointFile.runs`, the store's
`count_runs_by_checkpoint`, `runs` of the MCP `list_checkpoints` files). Sending a checkpoint to
a project is unchanged.

## Fix — every test green (October 31)

- e2e “GPU picker”: it expected a new draft without machine (“Choose a machine first”), but a
  draft always gets one (the last one used in the project, else the first allowed), since the
  first version of the interface: the test failed whatever ran before it. That step is gone;
  the test still checks the GPU rows, the CUDA_VISIBLE_DEVICES toggles, Escape closing only the
  picker, and an unreachable machine.
- CI, front-end job: `ubuntu-latest` became Ubuntu 26.04, on which Playwright 1.56 cannot install
  Chromium (“does not support chromium on ubuntu26.04-x64”): the job failed before running any
  test. Pinned to `ubuntu-24.04` until Playwright is upgraded.
- Once the job ran again, “unreachable machine” failed in CI (not locally): **a real bug**. The
  draft cancels itself on Escape unless the focused element says its list is open
  (`aria-expanded`); right after a picker (config, GPU) opens, the focus may still be on a field
  of the draft (the machine combobox, list closed): Escape then cancelled the whole draft
  instead of closing the picker. Fixed in `DraftRow`: Escape and Enter leave the draft alone
  while any floating list or picker (`.menu-content`) is open. New e2e test sending Escape to a
  field of the draft while the config picker is open (it failed before the fix).
- e2e “MLflow”: it took the first running row and expected a single stopped run in the project;
  it now follows the run it launched, by id (`launchedRow`).
- The e2e suite runs once on a fresh server (`.trainhub/e2e` is deleted at start): some tests
  rely on the state of a first pass (number of active runs, checkpoints deleted by the
  checkpoints test), so `--repeat-each` is not supported.
- Skipped locally only (they run in CI or need a setup): the local Docker test (Docker and the
  `trainhub-fake-train` image), the simulated SSH machines (`make sim-up`) and one test that
  needs a non-root user.

## Addition — link with Lookin through the MLflow run id (October 31)

- The MLflow run id is the **only** link between TrainHub and Lookin: no `trainhub.run_id` (or
  any other tag) is read, so the runs TrainHub did not launch link exactly like its own. Both
  tools already hold the id: TrainHub for its runs, external runs, run directories and
  inferences; Lookin for every experiment made from an MLflow run.
- One read-only route on each side, same path and same shape (`app`, `version`, `runs`: run id →
  entries with a `path` relative to the server that answered): `GET /api/links/mlflow-runs`.
  The caller puts the path behind the address *it* knows the other at, since Lookin and TrainHub
  are often reached under other names than the ones they bind; an entry whose path is not a
  page of the server (`//…`, `javascript:`) is dropped.
- TrainHub asks Lookin from the **server** (not the browser): no CORS to grant on Lookin, which
  has no login and refuses cross-site requests. Cached 30 s; the last answer stays with the
  error when Lookin is unreachable. `lookin.url` (from TrainHub) and `public_url` (from the
  browser) are two settings for the same reason.
- The "Lookin" button opens `?mlflow_run=<id>&mlflow_server=<tracking uri>`: Lookin asks only its
  own configured MLflow servers (the hinted one first), offers to add the hinted server when it
  is not one of them, and puts the run in a new or existing workspace with the same library calls
  as a drop on its Home page. Shown on the inferences, and on the finished runs of the projects
  taking a `{checkpoint}` (the inference projects); the badges show wherever a workspace uses
  the run.
- TrainHub's index lists the checkpoint sources the way the Checkpoints tab does (scans and
  MLflow searches cached 5 min): the first call after a start scans them. A source that fails is
  named in `errors` instead of failing the whole index.
- Deep links for Lookin's badges: `/p/<project>?run=<id>` and
  `/checkpoints/<source>?run=<directory>&inference=<MLflow run id>` scroll to the row and flash
  it, once per link.

## Change — inferences always listed; only the checkbox selects a checkpoint (November 1)

- An opened run shows every inference under its checkpoint, and those of deleted checkpoints at
  the bottom, without another click: `⇢ N` on the run and on the checkpoint is a count, no
  longer a button.
- A click on a checkpoint's line no longer selects it (to send, or to delete): only its checkbox
  does, so that a click meant for a badge, a link or the path does not select by accident. In
  pick mode (choosing the checkpoint of a draft), which has no checkbox, the click on the line
  still picks.
