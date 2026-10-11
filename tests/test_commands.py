"""Construction des commandes et échappement à chaque niveau d'imbrication."""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from trainhub.core import commands as c
from trainhub.core.commands import RunSpec, Target
from trainhub.core.inventory import load_inventory
from trainhub.core.models import Inventory, Project

from .conftest import ROOT


def spec_for(
    inv: Inventory,
    *,
    config: str = "baseline",
    command: str | None = None,
    overrides: tuple[str, ...] = (),
    machine: str = "gpu-01",
    venv: str = "main",
    project: Project | None = None,
    cuda: str | None = None,
) -> RunSpec:
    return RunSpec(
        run_id="r7f3k2",
        project=project or inv.project("segmentation"),
        machine=inv.machine(machine),
        image=inv.image("torch-2.4"),
        venv=inv.venv(venv),
        config=config,
        command=command,
        overrides=overrides,
        cuda_visible_devices=cuda,
    )


def test_example_matches_spec_shape() -> None:
    inv = load_inventory(ROOT / "inventory.example.yaml")
    argv = c.build_run(spec_for(inv))
    assert argv[:7] == [
        "ssh",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=10m",
        "-o",
        "ControlPath=~/.ssh/th-%C",
    ]
    assert argv[-3:-1] == ["alice@gpu-01.lab.local", "--"]
    remote = shlex.split(argv[-1])
    assert remote == [
        "docker", "run", "-d", "-t", "--init",
        "--name", "th-segmentation-r7f3k2",
        "--label", "trainhub=1",
        "--label", "trainhub.run_id=r7f3k2",
        "--label", "trainhub.project=segmentation",
        "--label", "trainhub.machine=gpu-01",
        "--label", "trainhub.image=torch-2.4",
        "--label", "trainhub.venv=main",
        "--label", "trainhub.config=baseline",
        "--gpus", "all", "--shm-size", "16g",
        "-v", "/data:/data:ro", "-v", "/home/alice/seg:/workspace/seg",
        "-w", "/workspace/seg", "-e", "WANDB_MODE=online",
        "--health-cmd", c.HEALTH_CMD, "--health-interval", "3s", "--health-retries", "1",
        "--health-timeout", "5s",
        "--ipc=host",
        "registry.lab/ml/torch:2.4-cuda12",
        "bash", "-lc", c.inner_script(spec_for(inv)),
    ]  # fmt: skip
    assert "--rm" not in remote
    assert "python train.py --config-name baseline & pid=$!" in remote[-1]


def test_keep_alive_can_be_disabled(inventory: Inventory) -> None:
    proj = inventory.project("segmentation")
    proj = proj.model_copy(update={"docker": proj.docker.model_copy(update={"keep_alive": False})})
    remote = c.docker_run(spec_for(inventory, project=proj))
    assert "--health-cmd" not in remote
    assert remote[-1] == (
        "source /opt/venvs/main/bin/activate && cd /workspace/seg"
        " && exec python train.py --config-name baseline"
    )


def _keep_alive_script(tmp_path: Path, inventory: Inventory, command: str) -> tuple[str, Path]:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "activate").write_text("")
    proj = inventory.project("segmentation").model_copy(
        update={"command": command + " {config}", "workdir": str(tmp_path)}
    )
    spec = dataclasses.replace(
        spec_for(inventory, project=proj),
        venv=inventory.venv("main").model_copy(update={"path": str(venv)}),
    )
    exit_file = tmp_path / "exit"
    return c.inner_script(spec).replace(c.EXIT_FILE, str(exit_file)), exit_file


def _wait_output(proc: subprocess.Popen[str], needle: str) -> str:
    assert proc.stdout is not None
    seen = ""
    for line in proc.stdout:
        seen += line
        if needle in line:
            return seen
    raise AssertionError(f"{needle!r} absent de la sortie : {seen!r}")


@pytest.mark.parametrize("code", [0, 3])
def test_keep_alive_script_keeps_container_and_returns_code(
    tmp_path: Path, inventory: Inventory, code: int
) -> None:
    import signal

    script, exit_file = _keep_alive_script(tmp_path, inventory, f"sh -c 'echo hi; exit {code}' --")
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        out = _wait_output(proc, "[trainhub] commande terminée")
        assert "hi" in out and f"(code {code})" in out
        assert exit_file.read_text().strip() == str(code)
        assert proc.poll() is None  # conteneur conservé
        proc.send_signal(signal.SIGTERM)  # fermeture : sort avec le code de la commande
        assert proc.wait(5) == code
    finally:
        proc.kill()


def test_keep_alive_script_forwards_sigterm(tmp_path: Path, inventory: Inventory) -> None:
    import signal

    cmd = "sh -c 'trap \"echo got TERM; exit 7\" TERM; echo ready; sleep 30 & wait' --"
    script, exit_file = _keep_alive_script(tmp_path, inventory, cmd)
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        _wait_output(proc, "ready")
        proc.send_signal(signal.SIGTERM)  # docker stop pendant la commande
        assert proc.wait(5) == 7
        assert not exit_file.exists()
    finally:
        proc.kill()


def test_shell_argv() -> None:
    argv = c.docker_exec_shell("th-x-r1", venv_path="/opt/venv/", workdir="/w s")
    assert argv[:6] == ["docker", "exec", "-it", "-e", "TERM=xterm-256color", "th-x-r1"]
    assert argv[6:8] == ["sh", "-c"]
    assert "cd '/w s'" in argv[8] and ". /opt/venv/bin/activate" in argv[8]


def test_tty_can_be_disabled(inventory: Inventory) -> None:
    proj = inventory.project("segmentation")
    proj = proj.model_copy(update={"docker": proj.docker.model_copy(update={"tty": False})})
    remote = c.docker_run(spec_for(inventory, project=proj))
    assert remote[:4] == ["docker", "run", "-d", "--init"]


def test_cuda_visible_devices(inventory: Inventory) -> None:
    remote = c.docker_run(spec_for(inventory, cuda="0,1"))
    i = remote.index("CUDA_VISIBLE_DEVICES=0,1")
    assert remote[i - 1] == "-e"
    with pytest.raises(ValueError, match="CUDA_VISIBLE_DEVICES"):
        c.docker_run(spec_for(inventory, cuda="0; rm -rf /"))


def test_local_machine_has_no_ssh(inventory: Inventory) -> None:
    argv = c.build_run(spec_for(inventory, machine="local"))
    assert argv[0] == "docker"
    assert not c.preview(spec_for(inventory, machine="local")).startswith("ssh")


def test_port_and_user(inventory: Inventory) -> None:
    m = inventory.machine("gpu-01").model_copy(update={"port": 2222})
    argv = c.build_ps(m)
    assert argv[argv.index("-p") + 1] == "2222"
    assert "alice@gpu-01.lab.local" in argv


def test_simple_builders(inventory: Inventory) -> None:
    t = Target(inventory.machine("gpu-02"), "th-x-r1")
    cases = {
        "logs": (c.build_logs(t, tail=50), ["docker", "logs", "-f", "--tail", "50", "th-x-r1"]),
        "inspect": (c.build_inspect(t), ["docker", "inspect", "--type", "container", "th-x-r1"]),
        "pause": (c.build_pause(t), ["docker", "pause", "th-x-r1"]),
        "unpause": (c.build_unpause(t), ["docker", "unpause", "th-x-r1"]),
        "stop": (c.build_stop(t, 30), ["docker", "stop", "-t", "30", "th-x-r1"]),
        "start": (c.build_start(t), ["docker", "start", "th-x-r1"]),
        "rm": (c.build_rm(t), ["docker", "rm", "th-x-r1"]),
    }
    for name, (argv, expected) in cases.items():
        assert argv[-3:-1] == ["gpu-02", "--"], name
        assert shlex.split(argv[-1]) == expected, name
    ps = shlex.split(c.build_ps(inventory.machine("gpu-02"))[-1])
    assert ps[-2:] == ["--format", "{{json .}}"]
    assert "label=trainhub=1" in ps


def test_list_configs(inventory: Inventory) -> None:
    det = inventory.project("detection")
    assert det.configs.discover is not None
    argv = c.build_list_configs(inventory.machine("gpu-01"), det.configs.discover)
    assert shlex.split(argv[-1]) == [
        "find", "-L", "/home/alice/det/configs", "-maxdepth", "10", "-type", "f",
        "-name", "*.yaml",
    ]  # fmt: skip


def test_parse_config_listing() -> None:
    from trainhub.core.models import ConfigDiscover

    d = ConfigDiscover(path="/cfg/", exclude=["hydra/*", "*/_*"])
    out = "\n".join(
        [
            "/cfg/baseline.yaml",
            "/cfg/exp/sweep/unet large.yaml",
            "/cfg/exp/_defaults.yaml",
            "/cfg/hydra/launcher.yaml",
            "/cfg/.hydra/config.yaml",
            "/cfg/notes.txt",
            "/elsewhere/x.yaml",
            "",
        ]
    )
    assert c.parse_config_listing(out, d) == ["baseline", "exp/sweep/unet large"]


def test_discover_root_home_relative() -> None:
    from trainhub.core.models import ConfigDiscover

    d = ConfigDiscover.model_validate({"root": "~/seg/configs"})
    assert c.discover_root(d) == "seg/configs"
    assert c.list_configs(d)[2] == "seg/configs"
    assert c.parse_config_listing("seg/configs/a/b.yaml\n", d) == ["a/b"]


def test_config_name_placeholder(inventory: Inventory) -> None:
    spec = spec_for(
        inventory,
        config="exp/sweep/unet",
        command="python t.py --config-path /cfg/{config} --config-name {config_name}",
    )
    assert c.resolved_command(spec) == (
        "python t.py --config-path /cfg/exp/sweep/unet --config-name unet"
    )
    with pytest.raises(ValueError, match="chemin relatif"):
        c.docker_run(spec_for(inventory, config="../etc/passwd"))


def test_resolve_placeholders() -> None:
    out = c.resolve_command(
        "run {project}/{machine} {config} id={run_id} ${HOME} '${oc.env:X}'",
        config="a b",
        run_id="r1",
        machine="m",
        project="p",
    )
    assert out == "run p/m 'a b' id=r1 ${HOME} '${oc.env:X}'"


def test_preview_lists_every_part(inventory: Inventory) -> None:
    text = c.preview(spec_for(inventory))
    assert text.splitlines()[0].startswith("ssh -o ControlMaster=auto")
    assert "alice@gpu-01.lab.local --" in text
    assert "python train.py --config-name baseline & pid=$!" in text
    assert shlex.split(text.replace("\\\n", " "))[-1] == c.inner_script(spec_for(inventory))


@pytest.mark.parametrize("bad", ["", "-rf", "a\nb"])
def test_invalid_config_rejected(inventory: Inventory, bad: str) -> None:
    with pytest.raises(ValueError):
        c.build_run(spec_for(inventory, config=bad))


# --- échappement : exécution réelle des deux niveaux de shell ----------------------

TRICKY = [
    "baseline",
    "with space",
    "it's",
    'say "hi"',
    "$HOME",
    "a;b",
    "$(touch pwned)",
    "`id`",
    "a && b || c",
    "*.yaml",
    "back\\slash",
    "émoji ✓",
]

OVERRIDES = [
    ("+trainer.max_epochs=10",),
    ('model.name="a b"',),
    ("+trainer.max_epochs=10", 'model.name="a b"', "x='q'", "y=$HOME", "z=a;b"),
    ("paths.out=${oc.env:HOME}/out",),
]


def _make_stubs(tmp: Path) -> dict[str, str]:
    bindir = tmp / "bin"
    bindir.mkdir()
    for name in ("docker", "python"):
        p = bindir / name
        p.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    venv = tmp / "venv dir's"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text("export ACTIVATED=1\n")
    work = tmp / "work $dir"
    work.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return env


def _run_remote(remote_string: str, env: dict[str, str], cwd: Path) -> list[str]:
    """Simule le shell de connexion distant : ``sh -c "<chaîne reçue par ssh>"``."""
    out = subprocess.run(
        ["sh", "-c", remote_string], env=env, cwd=cwd, capture_output=True, text=True, check=True
    )
    result: list[str] = json.loads(out.stdout)
    return result


@pytest.mark.parametrize("config", TRICKY)
@pytest.mark.parametrize("overrides", [(), *OVERRIDES])
@pytest.mark.parametrize("keep_alive", [False, True])
def test_escaping_roundtrip(
    tmp_path: Path,
    inventory: Inventory,
    config: str,
    overrides: tuple[str, ...],
    keep_alive: bool,
) -> None:
    env = _make_stubs(tmp_path)
    venv = inventory.venv("main").model_copy(update={"path": str(tmp_path / "venv dir's")})
    proj = inventory.project("segmentation")
    proj = proj.model_copy(
        update={
            "workdir": str(tmp_path / "work $dir"),
            "docker": proj.docker.model_copy(update={"keep_alive": keep_alive}),
        }
    )
    spec = RunSpec(
        run_id="r1",
        project=proj,
        machine=inventory.machine("gpu-01"),
        image=inventory.image("torch-2.4"),
        venv=venv,
        config=config,
        overrides=overrides,
    )
    argv = c.build_run(spec)
    # Niveau 1 : la chaîne passée à ssh, ré-interprétée par le shell distant.
    docker_argv = _run_remote(argv[-1], env, tmp_path)
    assert docker_argv == c.docker_run(spec)[1:]
    # Niveau 2 : bash -lc SCRIPT dans le conteneur (bash -c ici pour garder le PATH).
    assert docker_argv[-3:-1] == ["bash", "-lc"]
    script = docker_argv[-1]
    if keep_alive:
        # Le conteneur resterait démarré : on s'arrête juste après la commande.
        script = script.replace(c.EXIT_FILE, str(tmp_path / "exit"))
        script = script.split("; trap 'exit $code'")[0]
    out = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
    )
    first = out.stdout.splitlines()[0]
    assert json.loads(first) == ["train.py", "--config-name", config, *overrides]
    assert not (tmp_path / "pwned").exists()


def test_make_spec_restrictions(inventory: Inventory) -> None:
    from trainhub.core.selection import SelectionError, make_spec

    ok = make_spec(
        inventory, run_id="r1", project="segmentation", machine="gpu-01",
        image="torch-nightly", venv="exp", config=" baseline ", cuda_visible_devices=" ",
    )  # fmt: skip
    assert ok.config == "baseline" and ok.cuda_visible_devices is None
    with pytest.raises(SelectionError, match="autorisées : gpu-01, gpu-02"):
        make_spec(
            inventory, run_id="r1", project="segmentation", machine="local",
            image="torch-2.4", venv="main", config="b",
        )  # fmt: skip
    with pytest.raises(SelectionError, match="disponibles : main"):
        make_spec(
            inventory, run_id="r1", project="segmentation", machine="gpu-01",
            image="torch-2.4", venv="exp", config="b",
        )  # fmt: skip


def test_cli_preview(capsys: pytest.CaptureFixture[str]) -> None:
    from trainhub.cli import main

    code = main([
        "--inventory", str(ROOT / "inventory.example.yaml"), "preview",
        "-p", "segmentation", "-m", "gpu-01", "-i", "torch-2.4", "-e", "main",
        "-c", "baseline", "--run-id", "r7f3k2", "--exact",
    ])  # fmt: skip
    assert code == 0
    out = capsys.readouterr().out.strip()
    assert shlex.split(out)[0] == "ssh"
    assert "th-segmentation-r7f3k2" in out


def test_free_params_substitution() -> None:
    kw = {"config": "base", "run_id": "r1", "machine": "m", "project": "p"}
    tpl = "python t.py --config-name {config} {options} --my_option {option2}"
    out = c.resolve_command(tpl, **kw, params={"options": "a=1 'b c' $HOME", "option2": "x;y"})
    assert out == "python t.py --config-name base a=1 'b c' '$HOME' --my_option 'x;y'"
    # paramètre vide ou absent : rien n'est inséré
    assert c.resolve_command(tpl, **kw) == "python t.py --config-name base  --my_option "
    with pytest.raises(ValueError, match="option2"):
        c.resolve_command(tpl, **kw, params={"option2": "'oops"})


def test_free_params_through_both_shells(tmp_path: Path, inventory: Inventory) -> None:
    env = _make_stubs(tmp_path)
    proj = inventory.project("segmentation").model_copy(
        update={
            "command": "python train.py --config-name {config} {options} --my_option {option2}",
            "workdir": str(tmp_path),
        }
    )
    params = {"options": "+a=1 'model.name=\"x y\"' $(touch pwned)", "option2": '"it\'s"'}
    spec = dataclasses.replace(
        spec_for(inventory, project=proj),
        venv=inventory.venv("main").model_copy(update={"path": str(tmp_path / "venv dir's")}),
        params=params,
    )
    docker_argv = _run_remote(c.build_run(spec)[-1], env, tmp_path)
    script = docker_argv[-1].replace(c.EXIT_FILE, str(tmp_path / "exit"))
    script = script.split("; trap 'exit $code'")[0]
    out = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout.splitlines()[0]) == [
        "train.py", "--config-name", "baseline",
        "+a=1", 'model.name="x y"', "$(touch", "pwned)",
        "--my_option", "it's",
    ]  # fmt: skip
    assert not (tmp_path / "pwned").exists()


def test_system_venv_has_no_activation(inventory: Inventory) -> None:
    spec = dataclasses.replace(spec_for(inventory), venv=inventory.venv("system"))
    script = c.inner_script(spec)
    assert "source" not in script and "activate" not in script
    assert script.startswith("rm -f /tmp/.trainhub-exit; cd /workspace/seg || exit 1; python")


def test_make_spec_params(inventory: Inventory) -> None:
    from trainhub.core.selection import SelectionError, make_spec

    base = {
        "run_id": "r1",
        "project": "segmentation",
        "machine": "gpu-01",
        "image": "torch-2.4",
        "venv": "system",
        "config": "baseline",
    }
    cmd = "python t.py {config} {options}"
    spec = make_spec(inventory, **base, command=cmd, params={"options": "a=1"})
    assert spec.params == {"options": "a=1"} and spec.venv.is_system
    assert make_spec(inventory, **base, command=cmd).params == {"options": ""}
    with pytest.raises(SelectionError, match="nope"):
        make_spec(inventory, **base, command=cmd, params={"nope": "1"})
