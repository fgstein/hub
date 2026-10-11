# TrainHub

TrainHub launches and monitors training commands on remote machines, from a web interface, an
MCP server (for agents) or plain Python.

Each run is built from 4 choices (**machine**, **Docker image**, **venv**, **config**) and runs
the chain:

```
ssh machine → docker run -d → source venv → cd workdir → <your command, with {config} filled in>
```

The command is yours: any script taking a config file works. [Hydra](https://hydra.cc) projects
are a common case, see the [examples](#examples).

## Features

- **Projects as tabs**, each with its own generic command, editable from the interface.
- **Config picker**: config files are discovered on the machine and shown as a tree, with the
  number of runs already launched on each one.
- **Run parameters**: any `{name}` in the command that is not a built-in placeholder becomes a
  text field in the launch form.
- **Live monitoring**: status, elapsed time, logs streamed into an xterm.js terminal (ANSI
  colors, progress bars).
- **Saved logs**: every run's logs are written to `$TRAINHUB_DATA_DIR/logs/<run>.log` as they
  come, and stay available after the container is deleted, a machine outage or a TrainHub
  restart.
- **Kept container**: when the command finishes, the container stays started (status succeeded
  or failed from the exit code) so that you can open a shell in it, then close it yourself.
- **Control**: pause, resume, stop, restart, relaunch (prefilled draft), close container, delete.
- **Visible command**: each run shows its resolved command, or the full one (ssh + `docker run`
  + script).
- **Agents**: the same actions over MCP (Streamable HTTP on `/mcp`, or stdio with `trainhub mcp`).
- **No GPU needed for development**: `fake` (in-memory simulation) and `local` (local Docker)
  modes.

The original specification is issue #1 of the repository; choices and deviations are listed in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Configuration

TrainHub is configured by two things:

1. **The inventory** (`inventory.yaml`): machines, images, venvs and projects. Read-only for
   TrainHub, edited by you.
2. **Environment variables**: server settings (port, token, data directory…), see
   [Environment variables](#environment-variables).

The inventory is validated at startup with readable errors (file, line, field) and
**hot-reloaded** when it changes. If the new file is invalid, the previous inventory stays active
and the error is shown in the page footer. A full commented example is in
[`inventory.example.yaml`](inventory.example.yaml).

What you change from the interface (project command, open tabs, last selections, runs) is stored
in a SQLite database, `$TRAINHUB_DATA_DIR/trainhub.sqlite`, not in the inventory.

### Where each path lives

A run involves two filesystems, and the inventory mixes paths from both:

| Field                       | Resolved on                     | Used by                          |
|-----------------------------|---------------------------------|----------------------------------|
| `projects[].configs.root`   | the **machine** (host, over SSH) | `find`, to list the configs      |
| `projects[].docker.volumes` | `host_path:container_path`      | `docker run -v`                  |
| `projects[].workdir`        | the **container**               | `cd` before the command          |
| `venvs[].path`              | the **container**               | `source <path>/bin/activate`     |

So the configs root must also be mounted in the container (through `volumes`, or baked into the
image) for your command to read the files. Mounting a directory at the same path on both sides
keeps things simple.

### Machines

```yaml
machines:
  - id: gpu-01                  # identifier used everywhere (letters, digits, _ . -)
    label: GPU 01 (4×A100)      # optional: display name
    host: gpu-01.lab.local      # hostname or alias from ~/.ssh/config
    user: alice                 # optional
    port: 22                    # optional
```

Commands run through the system `ssh` binary, so `~/.ssh/config`, the SSH agent and jump hosts
all apply. `host: localhost` (without `user`/`port`) runs on the TrainHub host, without SSH.

### Images

```yaml
images:
  - id: torch-2.4
    image: registry.lab/ml/torch:2.4-cuda12   # pulled by docker run on the machine if missing
```

### Venvs

```yaml
venvs:
  - id: main
    path: /opt/venvs/main       # path INSIDE the container
  - id: exp
    path: /opt/venvs/exp
    images: [torch-nightly]     # optional: only offered with these images
```

A **`system`** venv is always offered: nothing is activated (the image's Python, or `uv run …`
in the command).

### Projects

A project is a tab of the interface: a command, where to run it, and where its configs are.

```yaml
projects:
  - id: segmentation
    label: Segmentation                         # optional
    command: python train.py --config {config}  # must contain {config}
    workdir: /workspace/seg                     # optional: cd before the command (container)
    machines: [gpu-01, gpu-02]                  # optional: allowed subset (default: all)
    images: [torch-2.4]                         # optional, same
    venvs: [main]                               # optional, same (system is always added)
    docker:                                     # optional: docker run options
      gpus: all                                 # --gpus
      shm_size: 16g                             # --shm-size
      volumes: [/data:/data:ro, /home/alice/seg:/workspace/seg]
      env: {WANDB_MODE: online}
      extra_args: ["--ipc=host"]                # any other docker run argument
      tty: true                                 # default true: -t (colors, progress bars)
      stop_timeout: 30                          # default 30: docker stop -t
      keep_alive: true                          # default true: keep the container at the end
    configs:
      root: /home/alice/seg/configs             # configs root, on the machine
      pattern: "*.yaml"                         # optional (default *.yaml)
      exclude: ["outputs/*"]                    # optional: relative paths to ignore (glob)
      max_depth: 10                             # optional
      # static: [baseline]                      # optional: configs listed by hand
```

#### The command and its placeholders

The command is a shell line run with `bash -lc` in the container. TrainHub fills in these
placeholders, each value escaped with `shlex.quote`:

| Placeholder     | Value                                                                 |
|-----------------|-----------------------------------------------------------------------|
| `{config}`      | Chosen config: path relative to `configs.root`, **without extension** |
| `{config_name}` | Basename of the config only                                           |
| `{run_id}`      | Short run identifier generated at launch, e.g. `rn3ceg`               |
| `{machine}`     | Machine id                                                            |
| `{project}`     | Project id                                                            |

For `configs.root: /home/alice/seg/configs` and the file
`/home/alice/seg/configs/experiments/unet_large.yaml`:
`{config}` = `experiments/unet_large`, `{config_name}` = `unet_large`.

**Any other `{name}` is a run parameter.** The launch form shows one field per parameter, e.g.
`python train.py --config {config} {options} --seed {seed}`. A value is split as a shell would
(`a=1 b=2` gives two arguments, `"a b"` a single one), each word is escaped, and an empty field
inserts nothing. The last values are remembered per project.

`${VAR}` is left untouched (shell variables, or interpolations of tools such as OmegaConf).

Extra arguments can also be appended to the command at launch through the API, MCP and the CLI
(`trainhub launch … lr=0.1 epochs=5`). In the interface, use a run parameter (`… {options}`).

#### Config discovery

Every file matching `pattern` under `configs.root` is listed (recursively, `find` on the chosen
machine, 60 s cache, ↻ button to refresh) and shown as a tree: click the file you want. Each file
(and each directory, cumulatively) shows the number of runs already launched on it, with a green
dot if some are running. Hidden files and directories (`.git`, `.hydra`…) are ignored.

`root` is either absolute or relative to the SSH user's home (`~/…`). It is **not** relative to
`workdir`, which is a container path. Use an absolute path.

Pick `root` so that `{config}` is exactly what your command expects: the identifier is always
relative to `root` and has no extension. Add the extension back in the command if needed
(`--config configs/{config}.yaml`).

### Examples

#### Plain script taking a file path

`train.py --config path/to/file.yaml`, run from `/workspace/proj`, configs in
`/workspace/proj/configs`:

```yaml
- id: proj
  command: python train.py --config configs/{config}.yaml
  workdir: /workspace/proj
  docker:
    volumes: [/home/alice/proj:/workspace/proj]
  configs:
    root: /home/alice/proj/configs
```

#### Hydra: primary config (`--config-name`)

With `@hydra.main(config_path="configs", …)` in `train.py`, `--config-name` takes the path
relative to `config_path`, without extension. So `root` is the `configs` directory itself:

```yaml
- id: segmentation
  command: python train.py --config-name {config} hydra.run.dir=outputs/{run_id}
  workdir: /home/alice/seg
  docker:
    volumes: [/home/alice/seg:/home/alice/seg]
  configs:
    root: /home/alice/seg/configs
    exclude: ["hydra/*", "model/*", "data/*"]   # config groups, not primary configs
```

A primary config in a subdirectory must start with `# @package _global_`.

#### Hydra: config group (`exp=…`)

The project is launched with `python run.py exp=exp1/run1`, where `exp` is a Hydra config group:

```
/this/is/a/path/trainer/
├── run.py                      # @hydra.main(config_path="configs", config_name="config")
└── configs/
    ├── config.yaml
    └── exp/
        └── exp1/
            └── run1.yaml       # selected with exp=exp1/run1
```

A group option is named relative to the **group directory**, without extension. So `root` is
`configs/exp`, not `configs`:

```yaml
- id: trainer
  command: python run.py exp={config}
  workdir: /this/is/a/path/trainer
  docker:
    volumes: [/this/is/a/path/trainer:/this/is/a/path/trainer]
  configs:
    root: /this/is/a/path/trainer/configs/exp
```

Choosing `exp1/run1.yaml` in the tree gives `{config}` = `exp1/run1`, and the command
`python run.py exp=exp1/run1`. With `root: …/configs`, you would get `exp=exp/exp1/run1`, which
Hydra rejects.

`workdir` only matters for launching `run.py` (and for where Hydra writes `outputs/`): Hydra
resolves `config_path` from the location of `run.py`, not from the current directory.

### Kept container and logs

With `keep_alive` (default), the script launched in the container runs the command in the
background, forwards `SIGTERM` (“Stop” = `docker stop`), then, when the command finishes, writes
its code to `/tmp/.trainhub-exit` and keeps the container started. A healthcheck
(`--health-cmd`, every 3 s) then becomes `unhealthy`: this is how TrainHub detects the end and
reads the exit code, without any extra command. Closing the container makes it exit with the
command's code. Set `docker.keep_alive: false` to let the container exit with the command.

Logs are followed for every active run and written to `$TRAINHUB_DATA_DIR/logs/<run>.log`
(`<run>.done` once the backup is complete). If the Docker logs come back shorter than the backup
(rotation), the old version is kept as `<run>.prev.log`. Deleting a run keeps its backup.

---

## Quick start (development, fake mode)

Requirements: [uv](https://docs.astral.sh/uv/), Node 22+.

```bash
make install   # uv sync + npm install
make dev       # backend in fake mode (auto reload) + Vite HMR → http://localhost:5173
```

`make dev` uses `dev/inventory.dev.yaml`: 4 simulated machines (one flaky and one offline),
3 projects, realistic logs (a simulated Hydra training: banner, resolved config, progress bars,
metrics, warnings, random failures).

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
`list_runs`, `get_run`, `get_logs`, `wait_run`, `get_gpus`, `pause_run`, `resume_run`,
`stop_run`, `restart_run`, `relaunch_run`, `close_container`, `delete_run`,
`set_project_command`) call the same service layer as the interface: a run launched by an agent
shows up immediately in the interface, with an “agent” icon. `get_logs` returns cleaned text
(no ANSI, progress bars reduced to their last state, truncated with a note) and a cursor: passed
back as `since`, only the new output is returned. `wait_run` waits on the server side until a
run finishes, a log line matches a regex or its status changes (progress notifications while
waiting). `get_gpus` runs `nvidia-smi` on the machines and shows the TrainHub runs on each GPU.

```bash
# Streamable HTTP (running server)
claude mcp add --transport http trainhub http://127.0.0.1:8420/mcp
# with a token
claude mcp add --transport http trainhub http://127.0.0.1:8420/mcp --header "Authorization: Bearer $TRAINHUB_TOKEN"

# stdio: relays to the running server (no second instance with its own state)
claude mcp add trainhub -- trainhub mcp --url http://127.0.0.1:8420
```

**OpenCode**: `mcp` section of `opencode.json` (or `opencode.jsonc`) at the project root.

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    // Streamable HTTP (running server)
    "trainhub": {
      "type": "remote",
      "url": "http://127.0.0.1:8420/mcp",
      "enabled": true
    }
  }
}
```

With a token, add the header (read from the environment) and disable OpenCode's automatic
OAuth detection:

```jsonc
"trainhub": {
  "type": "remote",
  "url": "http://127.0.0.1:8420/mcp",
  "headers": { "Authorization": "Bearer {env:TRAINHUB_TOKEN}" },
  "oauth": false
}
```

Or over stdio (relays to the running server; `TRAINHUB_TOKEN` is read from the environment):

```jsonc
"trainhub": {
  "type": "local",
  "command": ["trainhub", "mcp", "--url", "http://127.0.0.1:8420"]
}
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
