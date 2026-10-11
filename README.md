# TrainHub

TrainHub launches and monitors [Hydra](https://hydra.cc) trainings on remote machines, from a
web interface, an MCP server (for agents) or plain Python.

Each run is built from 4 choices (**machine**, **Docker image**, **venv**, **config**) and runs
the chain:

```
ssh machine → docker run -d → source venv → cd workdir → python train.py --config-name XXX
```

When the command finishes, the container **stays started** (run status: succeeded or failed
depending on the exit code): you can open a shell in it from the interface, then close it
yourself (“Close container”). Can be disabled per project with `docker.keep_alive: false`.

- Projects as tabs, each with its own editable generic command. Placeholders filled in by
  TrainHub: `{config}`, `{config_name}`, `{run_id}` (identifier generated at launch, e.g.
  `rn3ceg`), `{machine}`, `{project}`. **Any other `{name}` is a run parameter**: the draft
  shows one field per parameter (e.g. `python train.py --config-name {config} {options}
  --my_option {option2}`). The value is split as a shell would (`a=1 b=2` = two arguments,
  `"a b"` = a single one), each word is escaped, an empty field inserts nothing; the last values
  are remembered per project.
- Venv chosen among those of the inventory, plus **`system`**, always offered: no venv
  activated (the image's Python, or `uv run …` in the command).
- Live monitoring: status, elapsed time, logs streamed into an xterm.js terminal (ANSI colors, tqdm bars).
- Logs of **every** run saved as they come in `$TRAINHUB_DATA_DIR/logs/<run>.log`: still shown
  and downloadable after the container is closed or deleted, a machine outage or a TrainHub
  restart.
- Command of each run visible in its panel: resolved generic command, or full command (ssh +
  `docker run` + script, including the project's Docker options).
- Interactive shell in a run's container (`docker exec -it`, venv activated, in the workdir),
  “shell” tab of the panel.
- Hydra overrides appended to the command through the API, MCP and the CLI
  (`trainhub launch … trainer.max_epochs=5`). In the interface, use a parameter (`… {options}`);
  the “overrides” field only shows up to relaunch a run that had some.
- Control: pause, resume, stop, restart, relaunch (opens a prefilled draft), closing the kept
  container, deletion.
- The same actions for an agent through MCP (Streamable HTTP on `/mcp`, or stdio with `trainhub mcp`).
- Development and tests without a GPU thanks to the `fake` (in-memory simulation) and `local` (local Docker) modes.

The original specification is issue #1 of the repository; choices and deviations are listed in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Quick start (development, fake mode)

Requirements: [uv](https://docs.astral.sh/uv/), Node 22+.

```bash
make install   # uv sync + npm install
make dev       # backend in fake mode (auto reload) + Vite HMR → http://localhost:5173
```

`make dev` uses `dev/inventory.dev.yaml`: 4 simulated machines (one flaky and one offline),
3 projects, realistic logs (Hydra banner, resolved config, progress bars, metrics, warnings,
random failures).

Other modes:

| Command          | Executor | What is real                                            |
|------------------|----------|---------------------------------------------------------|
| `make dev`       | `fake`   | Nothing: machines and containers simulated in memory    |
| `make dev-local` | `local`  | Local Docker, without SSH (`trainhub-fake-train` image) |
| `make dev-sim`   | `ssh`    | SSH + Docker, to 2 simulated machines (docker compose)  |

`make dev-sim` generates a test key (`dev/ssh/`); add the line
`Include <path>/dev/ssh/config` to `~/.ssh/config` so that the `sim-gpu-01/02` aliases resolve.

Quality:

```bash
make lint      # ruff, mypy --strict, biome, tsc
make test      # pytest + vitest
make e2e       # Playwright (fake mode)
make build     # front end + Python package
make image     # trainhub:latest Docker image
```

The Docker (`tests/test_local_docker.py`) and SSH (`tests/test_sim_ssh.py`) integration tests
are skipped automatically if Docker, the test image or the simulated machines are missing.

---

## Inventory (`inventory.yaml`)

Resources are declared in a read-only YAML file, validated at startup with readable errors
(file, line, field) and **hot-reloaded** when it changes (a file that became invalid is ignored:
the previous inventory stays active and the error is shown in the page footer).

Full commented example: [`inventory.example.yaml`](inventory.example.yaml).

```yaml
machines:
  - id: gpu-01
    host: gpu-01.lab.local      # or an alias from ~/.ssh/config; “localhost” = without SSH
    user: alice                 # optional
images:
  - id: torch-2.4
    image: registry.lab/ml/torch:2.4-cuda12
venvs:
  - id: main
    path: /opt/venvs/main        # path INSIDE the container
projects:
  - id: segmentation
    command: python train.py --config-name {config}
    workdir: /workspace/seg
    docker: {gpus: all, shm_size: 16g, volumes: [/data:/data:ro], tty: true, stop_timeout: 30}
    configs:
      root: /home/alice/seg/configs   # root of the Hydra configs on the machine (host side)
      exclude: ["hydra/*"]            # optional
```

**Choosing the config.** Every `*.yaml` file under the project root (recursively, through
`find` on the chosen machine, 60 s cache, ↻ button) is shown as a tree in the draft: click the
file you want. Each file (and each directory, cumulatively) shows the number of runs already
launched on that config, with a green dot if some are running. A config is the relative path
without extension (`experiments/unet_large`): this is what Hydra's `--config-name` expects when
`config_path` is the root (a primary config in a subdirectory must start with
`# @package _global_`). The `{config_name}` placeholder gives the basename only (`unet_large`).
Hidden directories and files (`.hydra`, `.git`…) are ignored.

State that can be changed from the interface (overridden project command, open tabs, last
selections, runs) lives in a SQLite database (`$TRAINHUB_DATA_DIR/trainhub.sqlite`).

**Kept container and logs.** With `keep_alive` (default), the script launched in the container
runs the command in the background, forwards `SIGTERM` (“Stop” = `docker stop`, as before),
then, when the command finishes, writes its code to `/tmp/.trainhub-exit` and keeps the
container started. A healthcheck (`--health-cmd`, every 3 s) then becomes `unhealthy`: this is
how TrainHub detects the end and reads the exit code, without any extra command. Closing the
container makes it exit with the command's code. Logs are followed permanently for every active
run and written to `$TRAINHUB_DATA_DIR/logs/<run>.log` (`<run>.done` once the backup is
complete); if the Docker logs come back shorter than the backup (rotation), the old version is
kept as `<run>.prev.log`. Deleting a run keeps its backup.

---

## Deployment (Docker)

TrainHub runs in a container and does not need local Docker: every `docker` command runs on the
remote machines through the system `ssh` binary (honoring `~/.ssh/config`, the SSH agent and
jump hosts; `ControlMaster`/`ControlPersist` reuse connections).

```bash
make image   # or: docker build -t trainhub:latest .

docker run -d --name trainhub --network host \
  -v ~/.ssh/config:/home/th/.ssh/config:ro \
  -v ~/.ssh/known_hosts:/home/th/.ssh/known_hosts:ro \
  -v $SSH_AUTH_SOCK:/ssh-agent -e SSH_AUTH_SOCK=/ssh-agent \
  -v ./inventory.yaml:/etc/trainhub/inventory.yaml:ro \
  -v trainhub-data:/var/lib/trainhub \
  trainhub:latest
```

Then open <http://127.0.0.1:8420>.

**Private keys instead of the SSH agent.** Mount the keys read-only and reference them in the
mounted SSH config (`IdentityFile /home/th/.ssh/id_ed25519`):

```bash
  -v ~/.ssh/id_ed25519:/home/th/.ssh/id_ed25519:ro \
```

Permissions expected by `ssh`: private key `600` (or `400`), `config` `600` or `644` not
writable by others, owned by the container user. The `th` user has UID 1000: if your files
belong to another UID, rebuild with
`docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) -t trainhub .`.
The `/home/th/.ssh` directory must stay writable (`ControlMaster` sockets): mount files, not
the whole directory.

### Environment variables

| Variable                | Default              | Purpose                                                        |
|-------------------------|----------------------|----------------------------------------------------------------|
| `TRAINHUB_INVENTORY`    | `./inventory.yaml`   | Path of the inventory (hot-reloaded)                           |
| `TRAINHUB_DATA_DIR`     | `./.trainhub`        | SQLite database directory                                      |
| `TRAINHUB_HOST`         | `127.0.0.1`          | Listen address                                                 |
| `TRAINHUB_PORT`         | `8420`               | Port                                                           |
| `TRAINHUB_EXECUTOR`     | `ssh`                | `ssh`, `local` or `fake`                                       |
| `TRAINHUB_TOKEN`        | —                    | If set: token required everywhere (API, WebSocket, `/mcp`)     |
| `TRAINHUB_MCP_READONLY` | —                    | `1`: only expose the read-only MCP tools                       |
| `TRAINHUB_STATIC_DIR`   | auto                 | Directory of the built front end (set in the image)            |
| `TRAINHUB_LOG_LEVEL`    | `INFO`               | Level of the server's JSON logs                                |

**Token.** With `TRAINHUB_TOKEN`, send `Authorization: Bearer <token>` (API, MCP), or sign in
through the `/login` page, which sets a cookie (browser, WebSocket).

---

## MCP server

The tools (`list_projects`, `list_resources`, `list_configs`, `preview_command`, `launch_run`,
`list_runs`, `get_run`, `get_logs`, `pause_run`, `resume_run`, `stop_run`, `restart_run`,
`relaunch_run`, `close_container`, `delete_run`, `set_project_command`) call the same service
layer as the interface: a run launched by an agent shows up immediately in the interface, with
an “agent” icon. `get_logs` returns cleaned text (no ANSI, progress bars reduced to their last
state, truncated with a note).

```bash
# Streamable HTTP (running server)
claude mcp add --transport http trainhub http://127.0.0.1:8420/mcp
# with a token
claude mcp add --transport http trainhub http://127.0.0.1:8420/mcp --header "Authorization: Bearer $TRAINHUB_TOKEN"

# stdio: relays to the running server (no second instance with its own state)
claude mcp add trainhub -- trainhub mcp --url http://127.0.0.1:8420
```

`TRAINHUB_MCP_READONLY=1` only exposes the read-only tools.

---

## Plain Python and CLI usage

```python
from trainhub import TrainHub

tb = TrainHub.from_inventory("inventory.yaml")          # executor: TRAINHUB_EXECUTOR (default ssh)
run = await tb.launch(project="segmentation", machine="gpu-01",
                      image="torch-2.4", venv="main", config="baseline")
async for chunk in tb.stream_logs(run.id):
    print(chunk.decode(), end="")
await tb.stop(run.id)
```

```bash
trainhub preview -p segmentation -m gpu-01 -i torch-2.4 -e main -c baseline trainer.max_epochs=5
trainhub launch  -p segmentation -m gpu-01 -i torch-2.4 -e main -c baseline --follow
trainhub ps [--all]
trainhub logs r7f3k2 [-f] [--clean]
trainhub stop r7f3k2           # also: pause, resume, rm
trainhub serve                 # web server (API, WebSocket, MCP, front end)
trainhub mcp                   # stdio MCP connected to the server
```

The CLI shares the server's SQLite database (`--data-dir` / `TRAINHUB_DATA_DIR`).

---

## Architecture

```
src/trainhub/
  core/        pure functions: models, inventory, command building (argv, shlex),
               docker ps/inspect parsing, durations, log cleaning — no I/O
  executors/   Executor interface; ssh (production), local (local docker), fake (simulation)
  service/     run lifecycle, SQLite, monitoring (1 docker ps / machine / 3 s), log hub
               (1 docker logs -f shared per run, 2 MB ring buffer), reconciliation, bus
  api/         FastAPI: REST, WebSocket /ws (logs as binary frames), token, static front end
  mcp/         FastMCP (HTTP on /mcp) and stdio proxy
  client.py    TrainHub class (plain Python); cli.py
web/           SolidJS + Vite + Tailwind v4 + Kobalte + xterm.js (theme inspired by OpenCode)
dev/           dev inventories, fake-train image, simulated SSH machines (docker compose)
```

Key rules: the API and MCP only call the service layer; `core` is testable without I/O; no
command is built by unescaped concatenation (`shlex.quote` escaping at every level: remote
shell, then `bash -lc` in the container); closing the browser or restarting TrainHub never
interrupts a training (detached containers, found again through their `trainhub*` labels).

## Third-party code

The front-end theme reuses tokens from OpenCode's theme system (MIT license), see
[`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES).
