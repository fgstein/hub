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
- **MLflow progress**: for projects tracked with MLflow, a progress bar (current epoch / maximum
  epoch) and a status dot colored by the training state: green while it goes well, orange when
  no metric came for a while (stalled?), red if it failed, blue once finished. See
  [MLflow](#mlflow).
- **External runs**: the MLflow runs of a project's experiments that TrainHub did not launch
  (started by hand, from a notebook…) show up in that project with an `external` badge.
- **Failures stay in sight**: a failed run stays among the active runs, red, until someone
  clicks **Seen**; the tab shows how many are waiting.
- **Machines tab**: CPU (every core), memory and GPU load of every machine of the inventory,
  with the TrainHub runs placed on each GPU, refreshed every 5 s; a click on a GPU prepares a run
  on it.
- **Series**: run the same command N times in a row in a single container, with some
  parameters (e.g. `{seed}`) drawn at random for each repetition. See [Series](#series).
- **Checkpoints tab**: the runs already executed on disk (by TrainHub or not), their checkpoints
  with sizes and the total per source, their MLflow run and the inferences made with them; send checkpoints to an inference project (`{checkpoint}`,
  `extends`), delete checkpoints or whole runs. See [Checkpoints](#checkpoints).
- **Control**: pause, resume, stop, restart, relaunch (prefilled draft), close container, delete,
  and **mark as finished** for a run stuck active whose machine or container is dead (nothing is
  run on the machine; the run becomes `failed` with an explicit error, and shows as running again
  if its container turns out to be alive when the machine answers).
- **Visible command**: each run shows its resolved command, or the full one (ssh + `docker run`
  + script).
- **Agents**: the same actions over MCP (Streamable HTTP on `/mcp`, or stdio with `trainhub mcp`).
- **No GPU needed for development**: `fake` (in-memory simulation) and `local` (local Docker)
  modes.

The original specification is issue #1 of the repository; choices and deviations are listed in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Install and run

The built web interface is committed in `web/dist`: installing only needs Python 3.11+ and
access to the Python packages (PyPI or a mirror); no Node, no Docker on the TrainHub side.

```bash
git clone https://github.com/fgstein/hub.git trainhub && cd trainhub
python3 -m venv .venv && .venv/bin/pip install .   # or: uv sync --no-dev
.venv/bin/trainhub serve                            # or: uv run --no-dev trainhub serve
```

Then open <http://127.0.0.1:8420>. TrainHub reads the inventory (see
[Configuration](#configuration)), uses your `~/.ssh/config` and SSH agent to reach the machines,
and keeps its database and logs in `./.trainhub` (`TRAINHUB_DATA_DIR`). Options: `--inventory`,
`trainhub serve --host/--port`, and the [environment variables](#environment-variables).

To try it without machines: `trainhub --executor fake --inventory dev/inventory.dev.yaml serve`.

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

TrainHub uses the inventory given by `--inventory` or `TRAINHUB_INVENTORY`. Without either, it
takes the first of these files that exists:

1. `./inventory.yaml` (current directory)
2. `$XDG_CONFIG_HOME/trainhub/inventory.yaml` (by default `~/.config/trainhub/inventory.yaml`)
3. `/etc/trainhub/inventory.yaml`

```bash
mkdir -p ~/.config/trainhub && cp inventory.example.yaml ~/.config/trainhub/inventory.yaml
```

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

**Docker command.** `docker` by default. For a wrapper, a path or `sudo`, set
`docker_command: sudo -n docker` at the top of the inventory, or per machine
(`docker_command: [/opt/bin/mydocker, --flag]`). The wrapper must accept the subcommands TrainHub
uses (`run`, `ps`, `logs`, `inspect`, `exec`, `pause`, `unpause`, `stop`, `start`, `restart`,
`rm`) with the same options and output as `docker` (notably `ps --format '{{json .}}'` and
`inspect`). Config discovery (`find`) is not affected.

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
      tty_size: [100, 40]                       # default: terminal columns, rows (tqdm)
      stop_timeout: 30                          # default 30: docker stop -t
      keep_alive: true                          # default true: keep the container at the end
    env_files: [.env]                           # optional: variables, exported (see below)
    source: [scripts/setup.sh]                  # optional: scripts sourced before the command
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
| `{checkpoint}`  | Chosen checkpoint, as seen in the container (see [Checkpoints](#checkpoints)) |

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

#### Files sourced before the command

`env_files` (variable files, `export AAA=bbb` or `AAA=bbb`: everything is exported to the
command, thanks to `set -a`) then `source` (scripts sourced as is), in order, inside the
container, after the venv is activated and after `cd workdir`. Paths are relative to `workdir`,
absolute, or `~/…`. A missing file fails the run with `trainhub: file to source not found: …`,
unless it is declared `{path: .env.local, optional: true}`. Works with and without `keep_alive`.

#### Terminal size and progress bars

With `tty: true`, the container is started with `docker run -d -t`: its pseudo-terminal has a
**0x0** size since nobody is attached, and tqdm then prints no progress bar at all. TrainHub sets
the size first (`stty cols … rows …`) and passes `COLUMNS`/`LINES`; adjust it with `tty_size`. If
the interface's terminal is narrower than that width, bar lines wrap and `\r` only rewrites the
last one: reduce `tty_size`.

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

#### Inheriting a project (`extends`)

A project can start from another one and only change what differs, typically an inference
command next to the training one:

```yaml
  - id: seg-infer
    label: Segmentation — inference
    extends: segmentation
    command: python infer.py --config-name {config} --ckpt {checkpoint}
```

Every field of the parent is inherited except `id` and `label`; a field given by the child
replaces the parent's, except `docker`, merged key by key (`docker: {shm_size: 4g}` keeps the
parent's volumes). Chains are allowed (`c` extends `b` extends `a`).

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

### Checkpoints

The **Checkpoints** tab (pinned at the left of the tab bar; also `/api/checkpoints` and the MCP
tools `list_checkpoint_sources` / `list_checkpoints`) manages the outputs of runs already
executed, launched by TrainHub or not: it lists the run directories found on disk,
their checkpoints with size and date, deletes them, and sends a checkpoint to a project whose
command contains `{checkpoint}` (inference).
Sources are declared at the top of the inventory, independently of the projects:

```yaml
checkpoints:
  - id: seg-outputs
    label: Segmentation outputs                 # optional
    machine: gpu-01                             # where the files are listed and deleted;
                                                # null or omitted: the TrainHub server itself
    root: /home/alice/seg/outputs               # absolute, on the machine
    layout: "{name}/checkpoints/{group}/*.ckpt" # default
```

`layout` describes the tree under `root`: `{name}` (first level) is a run, `{group}` an optional
grouping level (`topk`, `last`…), other directories are literal names (globs allowed), and the
last component is the checkpoint file pattern. `output/run_a/checkpoints/topk/epoch0000012.ckpt`
gives run `run_a`, group `topk`. A checkpoint under a run that does not follow the layout is
still listed, as an “other” file of the run. Hidden directories are ignored.

- **Listing**: a single `find` per source on its machine (GNU `find`, for `-printf`), cached
  5 minutes, ↻ to rescan. Each source, run and group shows its total size.
- **Deletion**: only files matching the pattern, under a run directory. The machine resolves
  each path (`realpath`) and refuses one that leaves the root (symbolic link); then `rm` with
  the SSH user (a permission error is reported per file). Group directories left empty are
  removed (`rmdir`), nothing else.
- **Sending to a project**: `{checkpoint}` receives the path **in the container**, translated
  from the machine path through the project's `docker.volumes` (the most specific mount wins).
  A checkpoint that no volume covers is passed as is (e.g. mounted by a `docker_command`
  wrapper at the same path). The run keeps the machine path: each
  checkpoint shows how many runs were launched with it.

Several sources may point to the same shared disk (NFS) from different machines.

**Link with MLflow.** With an `mlflow` block, each run directory is linked to the MLflow run that
logged its path, under a parameter or tag. The tab then shows a column with a green dot and a
link to the run in the MLflow UI (the blue MLflow mark; an orange `orphan` label: no MLflow run
found; a red `deleted` label: its MLflow runs are all in the MLflow trash, until `mlflow gc`), and
`MLflow 6/7` next to the root (directories linked / directories). Runs in the MLflow trash are
never orphans nor inferences. The search box also finds a directory from the
name or id of its MLflow run.

```yaml
mlflow:
  tracking_uri: http://mlflow.lab:5000     # required: the global MLflow server is used

checkpoints:
  - id: seg-outputs
    root: /home/alice/seg/outputs
    mlflow:
      match: params.callbacks/model_checkpoint/dirpath  # params.<key> or tags.<key>
      # experiments: [segmentation]        # default: those of the mlflow section
```

The value of `match` is a path containing the directory, e.g. the checkpoint directory that
Lightning logs as a hyperparameter (`/workspace/logs/train/runs/2026-10-30_14-02-11/checkpoints`).
It may be a path seen from the container: only its components are compared, and its last
component that is a run directory of the source wins. Every MLflow run having the key is read
(most recent first, up to 10,000), with the same 5-minute cache as the scan (↻ refreshes both).
A directory logged by several MLflow runs (resumes, relaunches) links to the most recent one; the
tooltip lists the others. If MLflow is unreachable, the last result stays displayed with ⚠.

**Orphans.** As soon as a source has `mlflow.match`, a last button, “Orphans”, lists the MLflow
runs (of the sources' searches) whose logged directory exists in **no** source: deleted since, or
written outside every source. Every source is scanned for this, so a run is listed only if no
disk has its directory. If a source cannot be scanned (machine unreachable, no earlier listing),
a banner warns that runs of its directories may be listed by mistake.

Tick orphans (or all those shown, after a search) then **drag the slider to the end** to delete
them in MLflow (`runs/delete`: MLflow trash, restorable until `mlflow gc`). The server deletes
only runs that are orphans at that moment, and nothing while a source cannot be scanned. Also
`/api/mlflow-orphans` (`POST /api/mlflow-orphans/delete`) and the MCP tool `list_mlflow_orphans`
(read only).

**Inferences.** Each run directory also lists the inferences made with its checkpoints: `⇢ N` on
the run and on each checkpoint. A click on `⇢ N` shows the inferences under their checkpoint
(hidden by default; on the run: those of all its checkpoints): status dot, name, config, logged
values, date, links to TrainHub and MLflow. Inferences of checkpoints deleted since are grouped
at the bottom of the run. They come from:

- the TrainHub runs launched with a `{checkpoint}` of the source (always, MLflow or not), with
  their project and config;
- with `mlflow.inference`, the MLflow runs, typically of a separate experiment, that logged the
  path of a checkpoint of the source. An MLflow run tagged `trainhub.run_id` (`run_tag`) by a
  TrainHub run is merged into it.

```yaml
checkpoints:
  - id: seg-outputs
    root: /home/alice/seg/outputs
    mlflow:
      match: params.callbacks/model_checkpoint/dirpath   # optional: the training runs
      inference:
        match: params.ckpt_path                # key holding the checkpoint path
        experiments: [seg-inference]           # default: those of the mlflow section
        show: [params.data.dataset, metrics.test/miou]   # params / tags / metrics shown
```

`show` takes `params.<key>`, `tags.<key>` or `metrics.<key>`; a metric shows its last value
(`12` rather than `12.0`, otherwise 6 significant digits). `match` stays a parameter or a tag.

As for the directories, the path may be seen from the container: from its last component that
is a run directory, the rest is the checkpoint. An inference whose checkpoint was deleted since
stays listed, struck through.

Without `machine` (the usual case: a shared volume), `find` and `rm` run directly on the host of
the TrainHub server, without SSH, with its user (in the Docker image, mount the directory into the
container at the same path). Sending such a checkpoint to a project keeps the last machine used
for that project in the draft. Set `machine` only for a directory visible from that machine alone:
`find` and `rm` then run there.

In the tab:

- one button per source with its **total size** and number of checkpoints (the tab shows the sum
  of all sources); runs collapsed, sorted by date, name or size, searchable (a search on a file
  name opens the runs on the matching checkpoints), “hide empty” for runs without checkpoint;
- the checkpoints of a run form a single list sorted by name, each with a small label giving its
  `{group}` (`topk`, `last`…; `other`: outside the layout);
- each checkpoint shows its size, its date (“writing?” if modified less than a minute ago) and
  `⟲ N`, the TrainHub runs launched with it (hover for the list, click to open the project);
- an opened run starts with a line giving its directory path, with a **copy path** button;
- an inference launched by TrainHub has a relaunch button: a new draft like that run (nothing is
  launched);
- **sending**: tick checkpoints (click on the line), choose a project taking a `{checkpoint}`,
  “Create N drafts”: one draft per checkpoint in that project, on the source's machine; the
  draft's `{checkpoint}` field can also be filled by hand;
- **deleting**: “Delete…” switches to delete mode (a separate selection); tick checkpoints or
  whole runs (box on the run line), the bar sums up what goes (groups, checkpoints, runs, size), then **drag the
  slider to the end** (released before, it goes back; keyboard: arrows). Escape leaves the mode.

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

### Series

A **series** runs the same command several times in a row, in the **same container**: the
next repetition starts when the previous one ends. In the draft, set the number of runs in the
`×` field next to “Launch”, and click the die next to a parameter to draw it at random for each
repetition, between two integer bounds (default `0` – `2147483647`, a usual seed range). Every
other field (machine, image, venv, config, GPU, other parameters) is the same for all the
repetitions.

- The values are drawn **once, at launch**, distinct for each repetition when the range allows
  it, and stored with the run: the run row shows the progress (`2/5`, one cell per repetition)
  and its tooltip the values and exit codes. The logs show
  `[trainhub] series run 2/5 started · seed=4821` before each repetition.
- **Failure**: by default the series stops at the first failed repetition (the next ones are not
  launched). Check “continue after a failure” to launch them anyway. The run's final code is 0 if
  every repetition succeeded, else the code of the last failure.
- **Stop** stops the repetition in progress and the whole series. **Restart** starts the series
  over with the same values; **Relaunch** (API, MCP) creates a new series with the same values,
  while “Relaunch…” in the interface prepares a draft with the same settings and new draws.
- A series is **a single run** for TrainHub (one container, one log, one row). Each repetition
  gets `TRAINHUB_SERIES_INDEX` (1, 2…), usable in the command as `${TRAINHUB_SERIES_INDEX}`,
  e.g. `hydra.run.dir=outputs/{run_id}-${TRAINHUB_SERIES_INDEX}`: `{run_id}` is the same for all
  the repetitions.
- With MLflow, each repetition creates its own MLflow run, tagged `trainhub.run_id` and
  `trainhub.series_index`; the progress bar follows the repetition in progress.

API, MCP (`launch_run`) and Python: `series: {count: 5, random: {seed: {min: 0, max: 99999}},
continue_on_failure: false}` (or explicit `values`, one dict per repetition). CLI:
`trainhub launch … -n 5 -R seed -R fold=0:4 [--continue-on-failure]`. At most 100 repetitions.

### MLflow

With an `mlflow` section, TrainHub reads, for each run, the matching MLflow run: its status, a
parameter giving the maximum epoch and a metric giving the current epoch. The interface then
shows a progress bar (`3/10`), a link to the run in the MLflow UI, and colors the status dot:

| Run (container) | MLflow run              | Dot                         |
|-----------------|-------------------------|-----------------------------|
| running         | `RUNNING` / `SCHEDULED` | green: training going well  |
| running         | `RUNNING`, no new metric for `stale_after_min` | orange: training stalled? |
| running / succeeded | `FINISHED`          | blue: training finished     |
| any             | `FAILED` / `KILLED`     | red: training failed        |
| failed          | any                     | red                         |

Container states (launching, paused, stopping, stopped, machine unreachable) keep their usual
color: a deliberate stop stays grey even though MLflow reports `KILLED`.

```yaml
mlflow:                          # global section (optional)
  tracking_uri: http://mlflow.lab:5000   # as seen from TrainHub
  # experiments: [segmentation]  # experiments searched (default: all); see External runs
  max_epochs_param: trainer.max_epochs   # MLflow parameter: maximum epoch (default max_epochs)
  epoch_metric: epoch            # MLflow metric: current epoch (latest value; default epoch)
  # epoch_offset: 1              # added to the metric, e.g. 1 if it counts from 0
  # run_tag: trainhub.run_id     # tag holding the TrainHub run id (default)
  # inject_env: true             # see below
  # stale_after_min: 30          # orange dot after this long without a new metric
  # poll_interval_s: 30          # MLflow polling period (global section only)

projects:
  - id: segmentation
    mlflow:                      # optional: overrides the global values for this project
      epoch_metric: trainer/epoch
      experiments: [segmentation]  # its external runs are shown in this project
  - id: llm
    mlflow: {enabled: false}     # no MLflow for this project
```

**Finding the MLflow run.** TrainHub looks for the MLflow run whose tag `trainhub.run_id`
(`run_tag`) equals the TrainHub run id. With `inject_env` (default), TrainHub passes
`MLFLOW_RUN_CONTEXT={"trainhub.run_id": "<run id>"}` (and `TRAINHUB_RUN_ID`) to the container:
MLflow adds the tags of this variable to every run it creates (`mlflow.start_run()`, Lightning's
`MLFlowLogger`…), so the training code needs no change. Otherwise, tag the run yourself, e.g.
`mlflow.set_tag("trainhub.run_id", os.environ["TRAINHUB_RUN_ID"])`. TrainHub reads MLflow
directly from its own host (`tracking_uri`); the training keeps logging to MLflow as it already
does.

**Polling.** Every `poll_interval_s` (30 s by default), TrainHub sends, per MLflow server, one
`runs/search` for the running runs of the experiments concerned and one for the runs started
since the previous round (which catches a run that started and failed in between): every run
reads its state from these two answers, whatever their number. A run that stops running is
then read once (`runs/get`); the experiment list is refreshed every minute.

**Failures stay in sight.** A run that fails — its command (`failed`), or its training (MLflow
`FAILED`/`KILLED`) — stays among the active runs, red, with a **Seen** button; the project tab
shows a red counter. Once seen (by anyone: the state is shared), it moves to the finished runs.
A deliberate stop and “Mark as finished” are not failures. API: `POST /api/runs/<id>/seen`;
MCP: `mark_run_seen`.

**External runs.** The running MLflow runs of the experiments a project names in its
`mlflow.experiments` (its own or inherited from the global section) that TrainHub did not launch
show up in that project, with an `external` badge: name, `mlflow.user`, experiment, progress,
link. When several projects name the same experiment, the first one in the inventory gets its
runs; an experiment no project names is not watched. At start-up, TrainHub also takes the
failures that happened since the oldest running run; afterwards every new run of these
experiments. TrainHub has no machine, container nor logs for them and **never changes them in
MLflow**: they follow the same rules (orange when stalled, a failure stays until **Seen**), and
the trash button, there whether the run is running or not, only removes one from TrainHub (it
does not come back). A process killed without MLflow knowing leaves its run `RUNNING` forever:
it turns orange, and **Seen** on an orange external run moves it to the finished runs (“stalled,
seen”) — back among the active runs if it logs a metric again. Cleaning MLflow up is
done from the [Checkpoints](#checkpoints) tab. A run whose `run_tag` names a TrainHub run that
no longer exists is an external run. API: `GET /api/external-runs[?project=]`,
`POST /api/external-runs/<id>/seen` (failure or stalled), `DELETE /api/external-runs/<id>`; MCP:
`list_external_runs`, `mark_run_seen`, `delete_external_run`.

**Timeouts.** A request to MLflow gets 10 s to answer (60 s for the searches of the
Checkpoints tab, pages of 1000 runs); a slow answer is reported as “MLflow did not answer within
N s”, not as an unreachable server. TrainHub sends at most 2 requests at a time to one MLflow
server, and sends a request once more when its connection is cut (never a deletion).

**Credentials.** TrainHub calls the MLflow REST API with the usual variables of its own
environment: `MLFLOW_TRACKING_TOKEN` (bearer), or `MLFLOW_TRACKING_USERNAME` and
`MLFLOW_TRACKING_PASSWORD`.

Runs are polled while active, and up to 10 minutes after they finish until MLflow reports a
final status. If MLflow is unreachable, the last values stay displayed with a warning.

### Machines tab

The **Machines** tab (left of the project tabs) shows, for every machine of the inventory:
CPU usage (overall and one cell per core, over 0.5 s), load average and memory, read from
`/proc` over SSH; and each GPU's utilization, memory and temperature (`nvidia-smi`), with the
TrainHub runs pinned to it. An unreachable machine shows the SSH error. A click on a GPU
prepares a draft on that machine with `CUDA_VISIBLE_DEVICES` set to that GPU (the project is
asked when several can run on the machine). The tab refreshes every
5 s while it is open; nothing is queried otherwise.

---

## Quick start (development, fake mode)

Requirements: [uv](https://docs.astral.sh/uv/), Node 22+.

**Version:** every requested change bumps the **minor** version (`0.4.2` → `0.5.0`), in
`pyproject.toml`, `src/trainhub/__init__.py`, `web/package.json` and `web/package-lock.json`
(then `uv lock`).

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
make zip       # ../trainhubX.Y.Z.zip: repo without git-ignored files (same content as a clone)
```

`web/dist` (the built front end) is committed so that a clone works without Node: after any
change under `web/`, run `make build-web` and commit `web/dist` with it. CI fails otherwise.

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
| `TRAINHUB_INVENTORY`    | see [Configuration](#configuration) | Path of the inventory (hot-reloaded)            |
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
`list_runs`, `get_run`, `get_logs`, `wait_run`, `get_gpus`, `get_machine_load`,
`list_checkpoint_sources`, `list_checkpoints`, `list_mlflow_orphans`, `pause_run`, `resume_run`,
`stop_run`, `force_finish_run`, `restart_run`, `relaunch_run`, `close_container`, `delete_run`,
`set_project_command`) call the same service layer as the interface: a run launched by an agent
shows up immediately in the interface, with an “agent” icon. `get_logs` returns cleaned text
(no ANSI, progress bars reduced to their last state, truncated with a note) and a cursor: passed
back as `since`, only the new output is returned. `wait_run` waits on the server side until a
run finishes, a log line matches a regex or its status changes (progress notifications while
waiting). `get_gpus` runs `nvidia-smi` on the machines and shows the TrainHub runs on each GPU;
`get_machine_load` adds the CPU and memory load. Runs carry their MLflow state (`mlflow`).

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

tb = TrainHub.from_inventory()   # inventory searched as in Configuration; executor: TRAINHUB_EXECUTOR (default ssh)
run = await tb.launch(project="segmentation", machine="gpu-01",
                      image="torch-2.4", venv="main", config="baseline")
async for chunk in tb.stream_logs(run.id):
    print(chunk.decode(), end="")
await tb.stop(run.id)
```

```bash
trainhub preview -p segmentation -m gpu-01 -i torch-2.4 -e main -c baseline trainer.max_epochs=5
trainhub launch  -p segmentation -m gpu-01 -i torch-2.4 -e main -c baseline --follow
trainhub launch  -p segmentation … -n 5 -R seed   # series: 5 runs in a row, {seed} drawn
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
