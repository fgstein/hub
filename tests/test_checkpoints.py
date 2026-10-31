"""Checkpoint sources: layout, find listing, deletion script (real shell), service, API, MCP."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from trainhub.api.app import create_app
from trainhub.config import Settings
from trainhub.core import checkpoints as ck
from trainhub.core.inventory import InventoryError, parse_inventory
from trainhub.core.models import (
    CheckpointSource,
    MlflowConfig,
    MlflowInfo,
    MlflowRunRef,
    Run,
    RunStatus,
)
from trainhub.executors.base import MachineUnreachableError
from trainhub.mcp.server import build_mcp
from trainhub.service import InvalidRequestError, NotFoundError, RunService, Selection
from trainhub.service.checkpoints import build_listing
from trainhub.service.mlflow import (
    HttpMlflow,
    MlflowError,
    MlflowHit,
    MlflowSource,
    format_metric,
)

from .conftest import BASIC_INVENTORY
from .helpers import SIMULATION, make_service, wait_for

ROOT = "/home/alice/seg/outputs"

CKPT_INVENTORY = (
    BASIC_INVENTORY
    + """
  - id: seg-infer
    label: Inference
    extends: segmentation
    command: python infer.py --config-name {config} --ckpt {checkpoint}
    docker:
      volumes: [/data:/data:ro, /home/alice/seg:/workspace/seg]
checkpoints:
  - id: seg
    label: Segmentation outputs
    machine: gpu-01
    root: /home/alice/seg/outputs
  - id: flat
    machine: gpu-02
    root: /srv/runs/
    layout: "{name}/*.pt"
"""
    + SIMULATION.format(duration=40, scale=20, failure=0.0)
    + """
  checkpoints:
    seg: [run_a, run_b, run_c]
"""
)


# -- layout ---------------------------------------------------------------------------


def test_parse_layout() -> None:
    lay = ck.parse_layout("{name}/checkpoints/{group}/*.ckpt")
    assert lay.dirs == ("checkpoints", "{group}") and lay.file == "*.ckpt"
    assert lay.depth == 4 and lay.group_index == 2
    flat = ck.parse_layout("{name}/*.pt")
    assert flat.depth == 2 and flat.group_index is None


@pytest.mark.parametrize(
    "bad",
    [
        "*.ckpt",
        "checkpoints/{name}/*.ckpt",
        "{name}/{group}/{group}/*.ckpt",
        "{name}/{epoch}/*.ckpt",
        "{name}/../*.ckpt",
        "{name}//*.ckpt",
        "{name}/**/*.ckpt",
        "{name}/.hidden/*.ckpt",
        "{name}/{group}",
    ],
)
def test_parse_layout_errors(bad: str) -> None:
    with pytest.raises(ValueError, match="layout"):
        ck.parse_layout(bad)


def test_place_and_group_dirs() -> None:
    lay = ck.parse_layout("{name}/checkpoints/{group}/*.ckpt")
    assert ck.place("r1/checkpoints/topk/epoch01.ckpt", lay) == ck.Placement("r1", "topk")
    assert ck.place("r1/model.ckpt", lay) == ck.Placement("r1", None)
    assert ck.place("r1/other/topk/e.ckpt", lay) == ck.Placement("r1", None)
    assert ck.place("r1/checkpoints/topk/e.txt", lay) is None
    assert ck.place("top.ckpt", lay) is None
    assert ck.is_group_dir("r1/checkpoints/topk", lay)
    assert not ck.is_group_dir("r1/checkpoints", lay)
    assert not ck.is_group_dir("r1/other/topk", lay)
    assert ck.group_dir("r1", "last", lay) == "r1/checkpoints/last"
    globbed = ck.parse_layout("{name}/ck*/{group}/*.ckpt")
    assert ck.group_dir("r1", "last", globbed) is None
    flat = ck.parse_layout("{name}/*.pt")
    assert ck.place("r1/a.pt", flat) == ck.Placement("r1", "")


def test_valid_relpath() -> None:
    assert ck.valid_relpath("r/checkpoints/topk/e.ckpt")
    for bad in ["", "/abs/e.ckpt", "r/../e.ckpt", "r/./e.ckpt", "r//e", "r/.hidden/e", "e.ckpt"]:
        assert not ck.valid_relpath(bad), bad
    assert not ck.valid_relpath("r/e\n.ckpt")


def test_container_path() -> None:
    vols = ["/data:/data:ro", "/home/alice/seg:/workspace/seg", "/home/alice/seg/out:/out", "v:/v"]
    assert ck.container_path("/home/alice/seg/x/e.ckpt", vols) == "/workspace/seg/x/e.ckpt"
    assert ck.container_path("/home/alice/seg/out/r/e.ckpt", vols) == "/out/r/e.ckpt"
    assert ck.container_path("/home/alice/seg", vols) == "/workspace/seg"
    assert ck.container_path("/home/alice/segx/e.ckpt", vols) is None
    assert ck.container_path("/home/alice/seg/a/../b.ckpt", vols) == "/workspace/seg/b.ckpt"
    assert ck.container_path("/elsewhere/e.ckpt", vols) is None


# -- listing --------------------------------------------------------------------------


def _src(layout: str = "{name}/checkpoints/{group}/*.ckpt") -> CheckpointSource:
    return CheckpointSource(id="s", machine="m", root="/out", layout=layout)


def test_parse_listing_and_build_tree() -> None:
    out = "\n".join(
        [
            "d\t1000.5\t0\trun_old",
            "d\t2000.0\t0\trun_new",
            "d\t2000.0\t0\trun_new/checkpoints",
            "d\t2000.0\t0\trun_new/checkpoints/last",
            "d\t2000.0\t0\trun_new/checkpoints/empty",
            "f\t2100.0\t300\trun_new/checkpoints/topk/epoch0000002.ckpt",
            "f\t2200.0\t100\trun_new/checkpoints/topk/epoch0000001.ckpt",
            "f\t2300.0\t50\trun_new/checkpoints/last/epoch0000003.ckpt",
            "f\t2400.0\t7\trun_new/final.ckpt",
            "f\t900.0\t5\tstray.ckpt",
            "d\t3000.0\t0\tempty_run",
            "garbage line",
            "f\tnan-ish\t1\tbad/x.ckpt",
            "f\t1.0\t1\t.hidden/x.ckpt",
        ]
    )
    entries = ck.parse_listing(out)
    assert len(entries) == 11
    lst = build_listing(_src(), entries)
    assert lst.size == 300 + 100 + 50 + 7 + 5 and lst.count == 5
    assert [r.name for r in lst.runs] == ["empty_run", "run_new", "run_old"]
    new = lst.runs[1]
    assert new.size == 457 and new.count == 4
    assert [g.name for g in new.groups] == ["empty", "last", "topk"]
    topk = new.groups[2]
    assert [f.name for f in topk.files] == ["epoch0000001.ckpt", "epoch0000002.ckpt"]
    assert topk.size == 400 and topk.path == "run_new/checkpoints/topk"
    assert [f.path for f in new.others] == ["run_new/final.ckpt"]
    assert lst.runs[0].count == 0 and lst.runs[0].mtime is not None
    assert [f.path for f in lst.others] == ["stray.ckpt"]


find_missing = shutil.which("find") is None or shutil.which("realpath") is None


def _tree(root: Path) -> None:
    for rel, size in [
        ("r1/checkpoints/topk/epoch0000001.ckpt", 10),
        ("r1/checkpoints/topk/epoch0000002.ckpt", 20),
        ("r1/checkpoints/last/epoch0000002.ckpt", 20),
        ("r1/checkpoints/last/notes.txt", 3),
        ("r1/.hydra/x.ckpt", 1),
        ("r2 with space/checkpoints/topk/epoch0000005.ckpt", 5),
        ("r2 with space/final.ckpt", 4),
    ]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    (root / "r3").mkdir()


@pytest.mark.skipif(find_missing, reason="find/realpath missing")
def test_real_find(tmp_path: Path) -> None:
    root = tmp_path / "out"
    _tree(root)
    lay = ck.parse_layout("{name}/checkpoints/{group}/*.ckpt")
    res = subprocess.run(ck.list_checkpoints(str(root), lay), capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    src = CheckpointSource(id="s", machine="m", root=str(root))
    lst = build_listing(src, ck.parse_listing(res.stdout))
    assert lst.count == 5 and lst.size == 10 + 20 + 20 + 5 + 4
    names = {r.name: r for r in lst.runs}
    assert set(names) == {"r1", "r2 with space", "r3"}
    assert [g.name for g in names["r1"].groups] == ["last", "topk"]
    assert names["r3"].count == 0


@pytest.mark.skipif(find_missing, reason="find/realpath missing")
def test_real_delete_script(tmp_path: Path) -> None:
    root = tmp_path / "out"
    _tree(root)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.ckpt").write_text("x")
    # a symlink inside the root pointing outside of it must not allow deleting there
    (root / "r1" / "escape").symlink_to(outside)
    files = [
        "r1/checkpoints/topk/epoch0000001.ckpt",
        "r1/checkpoints/topk/epoch0000002.ckpt",
        "r2 with space/final.ckpt",
        "r1/escape/keep.ckpt",
        "r1/checkpoints/topk/missing.ckpt",
        "r1/checkpoints",
    ]
    dirs = ["r1/checkpoints/topk", "r1/checkpoints/last"]
    argv = ck.delete_checkpoints(str(root), files, dirs)
    res = subprocess.run(argv, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    o = ck.parse_delete_output(res.stdout, files)
    assert set(o.deleted) == {
        "r1/checkpoints/topk/epoch0000001.ckpt",
        "r1/checkpoints/topk/epoch0000002.ckpt",
        "r2 with space/final.ckpt",
        "r1/checkpoints/topk/missing.ckpt",
    }
    assert o.failed == {
        "r1/escape/keep.ckpt": "outside the root",
        "r1/checkpoints": "is a directory",
    }
    assert o.removed_dirs == ["r1/checkpoints/topk"]  # last/ still has a file
    assert (outside / "keep.ckpt").exists()
    assert not (root / "r1/checkpoints/topk").exists()
    assert (root / "r1/checkpoints/last/epoch0000002.ckpt").exists()


@pytest.mark.skipif(find_missing or os.geteuid() == 0, reason="needs a non-root user")
def test_real_delete_permission_error(tmp_path: Path) -> None:
    root = tmp_path / "out"
    _tree(root)
    locked = root / "r1/checkpoints/last"
    locked.chmod(0o555)
    try:
        f = "r1/checkpoints/last/epoch0000002.ckpt"
        res = subprocess.run(ck.delete_checkpoints(str(root), [f]), capture_output=True, text=True)
        o = ck.parse_delete_output(res.stdout, [f])
        assert "Permission denied" in o.failed[f]
    finally:
        locked.chmod(0o755)


@pytest.mark.skipif(find_missing, reason="find/realpath missing")
def test_real_delete_runs_script(tmp_path: Path) -> None:
    root = tmp_path / "out"
    _tree(root)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.ckpt").write_text("x")
    (root / "link").symlink_to(outside)
    (root / "r1" / "escape").symlink_to(outside)  # links inside a run are not followed
    (root / "file.ckpt").write_text("x")
    names = ["r1", "r2 with space", "link", "file.ckpt", "missing"]
    res = subprocess.run(ck.delete_run_dirs(str(root), names), capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    o = ck.parse_delete_output(res.stdout, names)
    assert set(o.deleted) == {"r1", "r2 with space", "missing"}
    assert o.failed == {"link": "symbolic link", "file.ckpt": "not a directory"}
    assert sorted(p.name for p in root.iterdir()) == ["file.ckpt", "link", "r3"]
    assert (outside / "keep.ckpt").exists()


def test_valid_run_name() -> None:
    assert ck.valid_run_name("run_a") and ck.valid_run_name("r2 with space")
    for bad in ["", ".", "..", ".hydra", "a/b", "/abs", "a\nb"]:
        assert not ck.valid_run_name(bad), bad


def test_delete_missing_root_output() -> None:
    o = ck.parse_delete_output("root\t/nope\n", ["a/b.ckpt"])
    assert o.root_missing and o.failed == {"a/b.ckpt": "root not found"}


# -- inventory ------------------------------------------------------------------------


def test_inventory_extends_and_sources() -> None:
    inv = parse_inventory(CKPT_INVENTORY, "t.yaml")
    child = inv.project("seg-infer")
    parent = inv.project("segmentation")
    assert child.extends == "segmentation" and child.label == "Inference"
    assert child.workdir == parent.workdir and child.machines == parent.machines
    assert child.docker.gpus == "all" and child.docker.shm_size == "16g"
    assert child.docker.volumes == ["/data:/data:ro", "/home/alice/seg:/workspace/seg"]
    assert child.configs == parent.configs
    assert child.accepts_checkpoint and not parent.accepts_checkpoint
    src = inv.checkpoint_source("flat")
    assert src.root == "/srv/runs" and src.display == "flat"
    assert inv.checkpoint_source("seg").layout == "{name}/checkpoints/{group}/*.ckpt"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            "  - {id: x, extends: nope, command: 'a {config}'}\n",
            "extends unknown project 'nope'",
        ),
        (
            "  - {id: x, extends: y, command: 'a {config}'}\n"
            "  - {id: y, extends: x, command: 'a {config}'}\n",
            "extends cycle",
        ),
        (
            "checkpoints:\n  - {id: c, machine: nope, root: /x}\n",
            "unknown machine 'nope'",
        ),
        ("checkpoints:\n  - {id: c, machine: gpu-01, root: rel/x}\n", "absolute"),
        (
            "checkpoints:\n  - {id: c, machine: gpu-01, root: /x, layout: 'x/*.ckpt'}\n",
            "first component must be {name}",
        ),
    ],
)
def test_inventory_errors(extra: str, message: str) -> None:
    with pytest.raises(InventoryError, match=message):
        parse_inventory(BASIC_INVENTORY + extra, "t.yaml")


def test_extends_chain() -> None:
    inv = parse_inventory(
        BASIC_INVENTORY
        + "  - {id: a, extends: segmentation, command: 'a {config}', docker: {shm_size: 1g}}\n"
        + "  - {id: b, extends: a, docker: {env: {X: '1'}}}\n",
        "t.yaml",
    )
    b = inv.project("b")
    assert b.command == "a {config}" and b.docker.shm_size == "1g" and b.docker.env == {"X": "1"}
    assert b.docker.gpus == "all" and b.label is None


# -- service --------------------------------------------------------------------------


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[RunService]:
    s = make_service(tmp_path, text=CKPT_INVENTORY)
    await s.start()
    yield s
    await s.close()


INFER = Selection(
    project="seg-infer", machine="gpu-01", image="torch-2.4", venv="main", config="baseline"
)


async def test_service_listing_and_totals(svc: RunService) -> None:
    sources = {s.id: s for s in svc.checkpoints.sources()}
    assert sources["seg"].size is None and sources["seg"].label == "Segmentation outputs"
    lst = await svc.checkpoints.listing("seg")
    assert [r.name for r in lst.runs] == ["run_a", "run_b", "run_c"]
    assert lst.runs[2].count == 0  # crashed before its first checkpoint
    a = lst.runs[0]
    assert [g.name for g in a.groups] == ["last", "topk"]
    assert a.groups[1].count == 3 and all(f.name.startswith("epoch") for f in a.groups[1].files)
    assert [f.name for f in lst.runs[1].others] == ["model_final.ckpt"]
    assert lst.count == 4 + 5 and lst.size == sum(r.size for r in lst.runs) > 0
    total = svc.checkpoints.sources()[0]
    assert total.size == lst.size and total.count == lst.count and total.scanned_at
    assert svc.inventory_view().checkpoint_sources[0].size == lst.size
    flat = await svc.checkpoints.listing("flat")
    assert flat.count > 0 and all(g.name == "" for r in flat.runs for g in r.groups)
    with pytest.raises(NotFoundError, match="checkpoint source"):
        await svc.checkpoints.listing("nope")


async def test_service_delete(svc: RunService) -> None:
    events = svc.bus.subscribe()
    lst = await svc.checkpoints.listing("seg")
    topk = lst.runs[0].groups[1]
    last = lst.runs[0].groups[0]
    paths = [f.path for f in topk.files] + [last.files[0].path]
    res = await svc.checkpoints.delete("seg", paths)
    assert sorted(res.deleted) == sorted(paths) and not res.failed
    assert sorted(res.removed_dirs) == sorted([topk.path, last.path])  # type: ignore[type-var]
    assert res.freed == topk.size + last.size
    assert res.size == lst.size - res.freed and res.count == lst.count - 4
    after = await svc.checkpoints.listing("seg")
    assert after.runs[0].name == "run_a" and after.runs[0].groups == []
    rescan = await svc.checkpoints.listing("seg", refresh=True)
    assert rescan.size == res.size
    types = []
    while not events.queue.empty():
        ev = events.queue.get_nowait()
        assert ev is not None
        types.append(ev.type)
    assert "checkpoints.updated" in types


@pytest.mark.parametrize(
    "bad",
    [
        "/home/alice/seg/outputs/run_a/x.ckpt",
        "run_a/../../etc/passwd.ckpt",
        "run_a/checkpoints/topk/notes.txt",
        "toplevel.ckpt",
        "run_a/.hydra/x.ckpt",
    ],
)
async def test_service_delete_rejects(svc: RunService, bad: str) -> None:
    with pytest.raises(InvalidRequestError, match="not a checkpoint"):
        await svc.checkpoints.delete("seg", [bad])


async def test_service_delete_runs(svc: RunService) -> None:
    lst = await svc.checkpoints.listing("seg")
    run = lst.runs[0]
    empty = next(r for r in lst.runs if r.count == 0)
    res = await svc.checkpoints.delete_runs("seg", [run.name, empty.name, "nope"])
    assert sorted(res.deleted) == sorted([run.name, empty.name, "nope"]) and not res.failed
    assert res.freed == run.size and res.size == lst.size - run.size
    after = await svc.checkpoints.listing("seg")
    assert {r.name for r in after.runs} == {r.name for r in lst.runs} - {run.name, empty.name}
    rescan = await svc.checkpoints.listing("seg", refresh=True)
    assert rescan.size == res.size and rescan.count == res.count
    for bad in ["run_a/checkpoints", "..", ".hydra", ""]:
        with pytest.raises(InvalidRequestError, match="not a run directory"):
            await svc.checkpoints.delete_runs("seg", [bad])


async def test_inference_with_checkpoint(svc: RunService) -> None:
    lst = await svc.checkpoints.listing("seg")
    f = lst.runs[0].groups[1].files[0]
    host = f"{ROOT}/{f.path}"
    with pytest.raises(InvalidRequestError, match="takes a"):
        await svc.preview(INFER)
    with pytest.raises(InvalidRequestError, match="no \\{checkpoint\\}"):
        await svc.preview(INFER.model_copy(update={"project": "segmentation", "checkpoint": host}))
    # no volume covers it: the machine path is passed as is (mounted by a wrapper…)
    p = await svc.preview(INFER.model_copy(update={"checkpoint": "/elsewhere/e.ckpt"}))
    assert p.command.endswith("--ckpt /elsewhere/e.ckpt")
    with pytest.raises(InvalidRequestError, match="absolute"):
        await svc.preview(INFER.model_copy(update={"checkpoint": "rel/e.ckpt"}))

    sel = INFER.model_copy(update={"checkpoint": host})
    p = await svc.preview(sel)
    assert (
        p.command
        == f"python infer.py --config-name baseline --ckpt /workspace/seg/outputs/{f.path}"
    )
    assert f"trainhub.checkpoint={host}" in p.argv[-1] or any(
        f"trainhub.checkpoint={host}" in a for a in p.argv
    )
    run = await svc.launch(sel, wait=True)
    assert run.checkpoint == host
    await wait_for(lambda: _finished(svc, run.id), timeout=20)
    relaunched = await svc.relaunch(run.id, wait=True)
    assert relaunched.checkpoint == host


async def _finished(svc: RunService, run_id: str) -> bool:
    return (await svc.get_run(run_id)).status in (RunStatus.SUCCEEDED, RunStatus.FAILED)


async def test_project_view_flags(svc: RunService) -> None:
    views = {p.id: p for p in svc.inventory_view().projects}
    assert views["seg-infer"].accepts_checkpoint and views["seg-infer"].extends == "segmentation"
    assert "checkpoint" not in views["seg-infer"].params
    assert not views["segmentation"].accepts_checkpoint


# -- API and MCP ----------------------------------------------------------------------


def test_api(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=CKPT_INVENTORY)
    with TestClient(create_app(Settings(), svc)) as client:
        sources = client.get("/api/checkpoints").json()
        assert [s["id"] for s in sources] == ["seg", "flat"] and sources[0]["size"] is None
        lst = client.get("/api/checkpoints/seg").json()
        assert lst["count"] == 9
        path = lst["runs"][0]["groups"][0]["files"][0]["path"]
        r = client.post("/api/checkpoints/seg/delete", json={"paths": [path]})
        assert r.status_code == 200 and r.json()["deleted"] == [path]
        assert client.get("/api/checkpoints").json()[0]["count"] == 8
        r = client.post("/api/checkpoints/seg/delete", json={"paths": ["../x.ckpt"]})
        assert r.status_code == 422 and "not a checkpoint" in r.json()["detail"]
        assert client.post("/api/checkpoints/seg/delete", json={"paths": []}).status_code == 422
        run = lst["runs"][0]["name"]
        r = client.post("/api/checkpoints/seg/delete-runs", json={"names": [run]})
        assert r.status_code == 200 and r.json()["deleted"] == [run]
        assert run not in {x["name"] for x in client.get("/api/checkpoints/seg").json()["runs"]}
        r = client.post("/api/checkpoints/seg/delete-runs", json={"names": ["a/b"]})
        assert r.status_code == 422 and "not a run directory" in r.json()["detail"]
        assert client.get("/api/checkpoints/nope").status_code == 404
        inv = client.get("/api/inventory").json()
        assert [s["id"] for s in inv["checkpoint_sources"]] == ["seg", "flat"]


async def test_mcp(svc: RunService) -> None:
    async with create_connected_server_and_client_session(build_mcp(svc)._mcp_server) as c:

        async def call(tool: str, **args: Any) -> Any:
            res = await c.call_tool(tool, args)
            assert not res.isError, res.content[0].text
            sc = res.structuredContent
            return sc.get("result", sc) if sc is not None else res.content[0].text

        sources = await call("list_checkpoint_sources")
        assert [s["id"] for s in sources] == ["seg", "flat"]
        lst = await call("list_checkpoints", source="seg", name="RUN_A")
        assert lst["runs_total"] == 1 and set(lst["runs"][0]["groups"]) == {"last", "topk"}
        path = lst["runs"][0]["groups"]["topk"][0]["path"]
        projects = {p["id"]: p for p in await call("list_projects")}
        assert projects["seg-infer"]["accepts_checkpoint"] is True
        run = await call(
            "launch_run",
            project="seg-infer",
            machine="gpu-01",
            image="torch-2.4",
            venv="main",
            config="baseline",
            checkpoint=f"{lst['root']}/{path}",
        )
        assert run["status"] in ("running", "launching")
        detail = await call("get_run", run_id=run["id"])
        assert detail["checkpoint"] == f"{ROOT}/{path}"


# -- source on the TrainHub server (machine: null) ---------------------------------------


def test_inventory_source_without_machine() -> None:
    inv = parse_inventory(
        BASIC_INVENTORY + "checkpoints:\n  - {id: here, machine: null, root: /x}\n"
        "  - {id: here2, root: /y}\n",
        "t.yaml",
    )
    assert inv.checkpoint_source("here").machine is None
    assert inv.checkpoint_source("here2").machine is None


@pytest.mark.skipif(find_missing, reason="find/realpath missing")
async def test_server_source_runs_locally_with_ssh_executor(tmp_path: Path) -> None:
    from trainhub.executors.ssh import SshExecutor
    from trainhub.service.checkpoints import SERVER_MACHINE

    root = tmp_path / "out"
    _tree(root)
    ex = SshExecutor()
    lay = ck.parse_layout("{name}/checkpoints/{group}/*.ckpt")
    argv = ck.list_checkpoints(str(root), lay)
    assert ex.full_argv(SERVER_MACHINE, argv) == argv  # no ssh
    res = await ex.exec(SERVER_MACHINE, argv, timeout=10)
    assert res.ok, res.err
    assert len([e for e in ck.parse_listing(res.out) if e.kind == "f"]) == 5


async def test_service_server_source(tmp_path: Path) -> None:
    s = make_service(
        tmp_path,
        text=CKPT_INVENTORY.replace(
            "\ncheckpoints:\n", f"\ncheckpoints:\n  - {{id: here, root: {ROOT}}}\n", 1
        ),
    )
    await s.start()
    try:
        view = {v.id: v for v in s.checkpoints.sources()}["here"]
        assert view.machine is None
        lst = await s.checkpoints.listing("here")
        assert lst.error is None and lst.machine is None and lst.count > 0
        path = next(f.path for r in lst.runs for g in r.groups for f in g.files)
        res = await s.checkpoints.delete("here", [path])
        assert res.deleted == [path]
    finally:
        await s.close()


# -- MLflow ---------------------------------------------------------------------------

ML_INVENTORY = (
    CKPT_INVENTORY.replace(
        "    root: /home/alice/seg/outputs\n",
        "    root: /home/alice/seg/outputs\n"
        "    mlflow: {match: params.callbacks/model_checkpoint/dirpath}\n",
        1,
    )
    + "mlflow:\n  tracking_uri: http://mlflow.lab:5000\n  experiments: [seg]\n"
)


def test_path_from_run() -> None:
    names = {"run_a"}
    assert ck.path_from_run("/ws/out/run_a/checkpoints/last.ckpt", names) == (
        "run_a/checkpoints/last.ckpt"
    )
    assert ck.path_from_run("run_a//x.ckpt", names) == "run_a/x.ckpt"
    assert ck.path_from_run("/ws/out/run_b/x.ckpt", names) is None


def test_run_in_path() -> None:
    names = {"run_a", "run_b", "checkpoints"}
    assert ck.run_in_path("/workspace/out/run_a/checkpoints", {"run_a"}) == "run_a"
    assert ck.run_in_path("/workspace/out/run_a/ckpt/", names) == "run_a"
    assert ck.run_in_path("run_b", names) == "run_b"
    assert ck.run_in_path("C:\\out\\run_b\\x", names) == "run_b"
    assert ck.run_in_path("/workspace/out/run_z/x", names) is None
    # the last component that is a run wins
    assert ck.run_in_path("/x/run_a/run_b/checkpoints", {"run_a", "run_b"}) == "run_b"


def test_inventory_checkpoint_mlflow() -> None:
    inv = parse_inventory(ML_INVENTORY, "t.yaml")
    seg = inv.checkpoint_source("seg")
    assert seg.mlflow is not None
    assert seg.mlflow.match == "params.callbacks/model_checkpoint/dirpath"
    cfg = inv.checkpoint_mlflow(seg)
    assert cfg is not None and cfg.tracking_uri == "http://mlflow.lab:5000"
    assert cfg.experiments == ["seg"]
    assert inv.checkpoint_mlflow(inv.checkpoint_source("flat")) is None
    with pytest.raises(InventoryError, match=r"needs the global mlflow\.tracking_uri"):
        parse_inventory(ML_INVENTORY.rsplit("mlflow:\n", 1)[0], "t.yaml")
    for bad in ("dirpath", "metrics.x", "params.", "params.a`b"):
        with pytest.raises(InventoryError, match=r"expected|invalid MLflow key"):
            parse_inventory(ML_INVENTORY.replace("params.callbacks/model_checkpoint/dirpath", bad))
    with pytest.raises(InventoryError, match="'match' and/or 'inference'"):
        parse_inventory(
            ML_INVENTORY.replace("{match: params.callbacks/model_checkpoint/dirpath}", "{}")
        )


def _ml(
    run_id: str, path: str | None, start: int = 1_700_000_000_000, stage: str = "active"
) -> dict[str, Any]:
    params = [{"key": "callbacks/model_checkpoint/dirpath", "value": path}] if path else []
    return {
        "info": {
            "run_id": run_id,
            "run_name": f"name-{run_id}",
            "experiment_id": "7",
            "status": "FINISHED",
            "start_time": start,
            "lifecycle_stage": stage,
        },
        "data": {
            "params": [{"key": "lr", "value": "0.1"}, *params],
            "metrics": [
                {"key": "epoch", "value": 12.0, "step": 12},
                {"key": "test/miou", "value": 0.712345678, "step": 12},
            ],
        },
    }


async def test_http_search_by_key() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/2.0/mlflow/")
        if path == "experiments/get-by-name":
            return httpx.Response(200, json={"experiment": {"experiment_id": "7"}})
        assert path == "runs/search"
        body = json.loads(req.content)
        bodies.append(body)
        if "page_token" not in body:
            runs = [_ml("m1", "/ws/out/run_a/checkpoints"), _ml("m0", None)]
            return httpx.Response(200, json={"runs": runs, "next_page_token": "p2"})
        runs = [_ml("m2", "/ws/out/run_b/checkpoints", stage="deleted")]
        return httpx.Response(200, json={"runs": runs})

    client = HttpMlflow(transport=httpx.MockTransport(handler))
    cfg = MlflowConfig(tracking_uri="http://mlflow.lab:5000", experiments=["seg"])
    try:
        hits, truncated = await client.search_by_key(
            cfg,
            "params.callbacks/model_checkpoint/dirpath",
            ["params.lr", "tags.nope", "metrics.epoch", "metrics.test/miou", "metrics.nope"],
        )
    finally:
        await client.close()
    assert not truncated
    assert [(h.value, h.ref.run_id) for h in hits] == [
        ("/ws/out/run_a/checkpoints", "m1"),
        ("/ws/out/run_b/checkpoints", "m2"),
    ]
    assert hits[0].logged == {
        "params.lr": "0.1",
        "metrics.epoch": "12",
        "metrics.test/miou": "0.712346",
    }
    ref = hits[0].ref
    assert ref.run_name == "name-m1" and ref.status == "FINISHED" and ref.started_at
    assert ref.url == "http://mlflow.lab:5000/#/experiments/7/runs/m1"
    # runs in the MLflow trash are searched too, flagged
    assert not ref.deleted and hits[1].ref.deleted
    assert all(b["run_view_type"] == "ALL" for b in bodies)
    assert bodies[0]["filter"] == "params.`callbacks/model_checkpoint/dirpath` LIKE '%'"
    assert bodies[0]["experiment_ids"] == ["7"] and bodies[1]["page_token"] == "p2"


class _StubMlflow(MlflowSource):
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.inferences: list[MlflowHit] = []
        self.extra: list[MlflowHit] = []
        self.deleted: list[str] = []
        self.delete_error: str | None = None
        self.info = MlflowInfo(run_id="th-ml", experiment_id="9")
        self.inference: tuple[MlflowConfig, list[str]] | None = None

    async def fetch(self, cfg: MlflowConfig, run: Run) -> MlflowInfo:
        return self.info

    async def delete_run(self, cfg: MlflowConfig, run_id: str) -> None:
        if self.delete_error:
            raise MlflowError(self.delete_error)
        self.deleted.append(run_id)

    async def search_by_key(
        self, cfg: MlflowConfig, match: str, extra: Sequence[str] = ()
    ) -> tuple[list[MlflowHit], bool]:
        self.calls += 1
        if self.fail:
            raise MlflowError("MLflow unreachable")
        ref = MlflowRunRef
        if match == "params.ckpt_path":
            self.inference = (cfg, list(extra))
            return self.inferences, False
        return [
            *self.extra,
            MlflowHit("/ws/out/run_a/checkpoints", ref(run_id="new", status="RUNNING")),
            MlflowHit("/ws/out/run_a/checkpoints", ref(run_id="old", status="FAILED")),
            MlflowHit("/ws/out/run_c/checkpoints", ref(run_id="c")),
            MlflowHit("/ws/out/elsewhere/checkpoints", ref(run_id="x")),
        ], False


async def test_service_listing_with_mlflow(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=ML_INVENTORY)
    await svc.start()
    stub = _StubMlflow()
    svc.mlflow.source = stub
    try:
        lst = await svc.checkpoints.listing("seg")
        runs = {r.name: [m.run_id for m in r.mlflow_runs] for r in lst.runs}
        assert runs == {"run_a": ["new", "old"], "run_b": [], "run_c": ["c"]}
        assert lst.mlflow is not None and lst.mlflow.searched == 4 and not lst.mlflow.error
        assert lst.mlflow.match == "params.callbacks/model_checkpoint/dirpath"
        assert not any(m.deleted for r in lst.runs for m in r.mlflow_runs)
        # cached with the scan, refreshed with it
        await svc.checkpoints.listing("seg")
        assert stub.calls == 1
        # an outage keeps the last result, with the error
        stub.fail = True
        lst = await svc.checkpoints.listing("seg", refresh=True)
        assert stub.calls == 2 and lst.mlflow is not None
        assert lst.mlflow.error == "MLflow unreachable"
        assert [m.run_id for m in lst.runs[0].mlflow_runs] == ["new", "old"]
        # no mlflow section: no search
        flat = await svc.checkpoints.listing("flat")
        assert flat.mlflow is None and stub.calls == 2
    finally:
        await svc.close()


async def test_fake_mlflow_listing(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=ML_INVENTORY)
    await svc.start()
    try:
        lst = await svc.checkpoints.listing("seg")
        found = {r.name: bool(r.mlflow_runs) for r in lst.runs}
        assert found == {"run_a": True, "run_b": False, "run_c": True}
        assert lst.runs[0].mlflow_runs[0].url
    finally:
        await svc.close()


INF_INVENTORY = ML_INVENTORY.replace(
    "    mlflow: {match: params.callbacks/model_checkpoint/dirpath}\n",
    "    mlflow:\n"
    "      match: params.callbacks/model_checkpoint/dirpath\n"
    "      inference:\n"
    "        match: params.ckpt_path\n"
    "        experiments: [seg-infer]\n"
    "        show: [params.data]\n",
    1,
)


async def test_service_inferences(tmp_path: Path) -> None:
    """The inferences come from MLflow only, with their dataset and mapping."""
    text = INF_INVENTORY.replace(
        "        show: [params.data]\n",
        "        show: [params.data]\n"
        "        dataset_name: params.ds\n"
        "        dataset_path: params.ds_path\n"
        "        mapping_name: params.mapping\n",
        1,
    )
    svc = make_service(tmp_path, text=text)
    await svc.start()
    stub = _StubMlflow()
    svc.mlflow.source = stub
    try:
        lst = await svc.checkpoints.listing("seg")
        a = next(r for r in lst.runs if r.name == "run_a")
        ckpt = a.groups[1].files[0]
        ref = MlflowRunRef
        stub.inferences = [
            MlflowHit(
                f"/workspace/seg/outputs/{ckpt.path}",
                ref(run_id="in", run_name="infer-in"),
                {
                    "params.data": "ade20k",
                    "params.ds": "ade20k-val",
                    "params.ds_path": "/data/ade/val",
                    "params.mapping": "ade-150",
                },
            ),
            # on a checkpoint deleted since
            MlflowHit(
                "/elsewhere/run_a/checkpoints/topk/epoch0000999.ckpt",
                ref(run_id="gone", run_name="infer-gone"),
                {"params.data": "coco"},
            ),
            MlflowHit("/elsewhere/unknown/x.ckpt", ref(run_id="lost")),
            # in the MLflow trash: not shown
            MlflowHit(f"/w/{ckpt.path}", ref(run_id="trash", deleted=True)),
        ]
        lst = await svc.checkpoints.listing("seg", refresh=True)
        assert stub.inference is not None
        cfg, extra = stub.inference
        assert cfg.experiments == ["seg-infer"]
        assert extra == ["params.data", "params.ds", "params.ds_path", "params.mapping"]
        a = next(r for r in lst.runs if r.name == "run_a")
        by_ml = {i.mlflow.run_id: i for i in a.inferences}
        assert set(by_ml) == {"in", "gone"}
        inf = by_ml["in"]
        assert inf.checkpoint == ckpt.path and inf.exists and inf.mlflow.run_name == "infer-in"
        assert (inf.dataset_name, inf.dataset_path, inf.mapping_name) == (
            "ade20k-val",
            "/data/ade/val",
            "ade-150",
        )
        assert inf.logged == {"params.data": "ade20k"}  # the special keys have their badges
        gone = by_ml["gone"]
        assert (
            not gone.exists and gone.dataset_name is None and gone.logged == {"params.data": "coco"}
        )
        assert gone.checkpoint == "run_a/checkpoints/topk/epoch0000999.ckpt"
        assert all(not r.inferences for r in lst.runs if r.name != "run_a")
        assert lst.mlflow_inference is not None and lst.mlflow_inference.searched == 4
        assert lst.mlflow_inference.match == "params.ckpt_path"
    finally:
        await svc.close()


async def test_trainhub_runs_are_not_inferences(svc: RunService) -> None:
    """MLflow is the only source of the inferences: a TrainHub run launched with a checkpoint
    is not one."""
    lst = await svc.checkpoints.listing("seg")
    ckpt = lst.runs[0].groups[1].files[0]
    await svc.launch(INFER.model_copy(update={"checkpoint": f"{ROOT}/{ckpt.path}"}), wait=True)
    lst = await svc.checkpoints.listing("seg")
    assert lst.runs[0].inferences == []
    assert lst.mlflow is None and lst.mlflow_inference is None


async def test_training_show(tmp_path: Path) -> None:
    text = ML_INVENTORY.replace(
        "    mlflow: {match: params.callbacks/model_checkpoint/dirpath}\n",
        "    mlflow:\n"
        "      match: params.callbacks/model_checkpoint/dirpath\n"
        "      show: [metrics.val_loss, params.lr]\n",
        1,
    )
    svc = make_service(tmp_path, text=text)
    await svc.start()
    try:
        lst = await svc.checkpoints.listing("seg")
        a = next(r for r in lst.runs if r.name == "run_a")
        assert set(a.mlflow_logged) == {"metrics.val_loss", "params.lr"}
        assert next(r for r in lst.runs if r.name == "run_b").mlflow_logged == {}
        with pytest.raises(InventoryError, match=r"params\.<key>, tags\.<key> or metrics"):
            parse_inventory(text.replace("show: [metrics.val_loss", "show: [val_loss"), "t.yaml")
    finally:
        await svc.close()


async def test_fake_mlflow_inferences(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=INF_INVENTORY)
    await svc.start()
    try:
        lst = await svc.checkpoints.listing("seg")
        a = next(r for r in lst.runs if r.name == "run_a")
        assert len(a.inferences) == 2
        assert {i.exists for i in a.inferences} == {True, False}
        assert all(set(i.logged) == {"params.data"} for i in a.inferences)
    finally:
        await svc.close()


async def test_fake_mlflow_inference_metrics(tmp_path: Path) -> None:
    text = INF_INVENTORY.replace("show: [params.data]", "show: [params.data, metrics.epoch]")
    svc = make_service(tmp_path, text=text)
    await svc.start()
    try:
        lst = await svc.checkpoints.listing("seg")
        a = next(r for r in lst.runs if r.name == "run_a")
        assert a.inferences
        assert all(set(i.logged) == {"params.data", "metrics.epoch"} for i in a.inferences)
        assert all(i.logged["metrics.epoch"].isdigit() for i in a.inferences)
    finally:
        await svc.close()


def test_inventory_inference_settings() -> None:
    inv = parse_inventory(INF_INVENTORY, "t.yaml")
    seg = inv.checkpoint_source("seg")
    cfg = inv.checkpoint_mlflow(seg, inference=True)
    assert cfg is not None and cfg.experiments == ["seg-infer"] and cfg.run_tag == "trainhub.run_id"
    assert inv.checkpoint_mlflow(seg) is not None
    assert inv.checkpoint_mlflow(inv.checkpoint_source("flat"), inference=True) is None
    with pytest.raises(InventoryError, match=r"params\.<key>, tags\.<key> or metrics\.<key>"):
        parse_inventory(INF_INVENTORY.replace("show: [params.data]", "show: [data]"), "t.yaml")
    # metrics may be shown, not matched
    shown = INF_INVENTORY.replace("show: [params.data]", "show: [params.data, metrics.epoch]")
    seg = parse_inventory(shown, "t.yaml").checkpoint_source("seg")
    assert seg.mlflow is not None and seg.mlflow.inference is not None
    assert seg.mlflow.inference.show == ["params.data", "metrics.epoch"]
    with pytest.raises(InventoryError, match=r"params\.<key> or tags\.<key>"):
        parse_inventory(
            INF_INVENTORY.replace("match: params.ckpt_path", "match: metrics.x"), "t.yaml"
        )


async def test_mlflow_orphans(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=ML_INVENTORY)
    await svc.start()
    stub = _StubMlflow()
    svc.mlflow.source = stub
    try:
        lst = await svc.checkpoints.mlflow_orphans()
        assert lst.enabled and lst.searched == 4 and lst.linked == 3
        [o] = lst.runs
        assert o.run.run_id == "x" and o.path == "/ws/out/elsewhere/checkpoints"
        assert o.sources == ["seg"] and o.match == "params.callbacks/model_checkpoint/dirpath"
        assert not lst.scan_errors and not lst.mlflow_errors
        # a directory of another source (flat, without mlflow) is not an orphan
        flat = await svc.checkpoints.listing("flat")
        flat_hit = MlflowHit(f"/mnt/{flat.runs[0].name}/x", MlflowRunRef(run_id="f"))
        stub.extra = [flat_hit]
        lst = await svc.checkpoints.mlflow_orphans(refresh=True)
        assert [o.run.run_id for o in lst.runs] == ["x"] and lst.linked == 4
        # runs in the MLflow trash: never orphans, still linked to their directory
        trash = MlflowRunRef(run_id="t", deleted=True)
        stub.extra = [
            MlflowHit("/ws/out/gone/checkpoints", MlflowRunRef(run_id="g", deleted=True)),
            MlflowHit("/ws/out/run_b/checkpoints", trash),
        ]
        lst = await svc.checkpoints.mlflow_orphans(refresh=True)
        assert [o.run.run_id for o in lst.runs] == ["x"] and lst.searched == 4
        seg = await svc.checkpoints.listing("seg")
        assert {r.name: r.mlflow_runs for r in seg.runs}["run_b"] == [trash]
        # MLflow down: the error is reported
        stub.fail = True
        svc.checkpoints.clear()
        lst = await svc.checkpoints.mlflow_orphans(refresh=True)
        assert lst.mlflow_errors == {"seg": "MLflow unreachable"} and not lst.runs
    finally:
        await svc.close()


async def test_mlflow_orphans_disabled(svc: RunService) -> None:
    lst = await svc.checkpoints.mlflow_orphans()
    assert not lst.enabled and not lst.runs and lst.searched == 0


async def test_mlflow_orphans_scan_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = make_service(tmp_path, text=ML_INVENTORY)
    await svc.start()
    svc.mlflow.source = _StubMlflow()
    real = svc.executor.exec

    async def exec_(machine: Any, argv: list[str], **kw: Any) -> Any:
        if machine.id == "gpu-01":
            raise MachineUnreachableError("gpu-01", "unreachable")
        return await real(machine, argv, **kw)

    monkeypatch.setattr(svc.executor, "exec", exec_)
    try:
        lst = await svc.checkpoints.mlflow_orphans()
        # the directories of seg are unknown: its MLflow runs all look like orphans
        assert set(lst.scan_errors) == {"seg"} and "unreachable" in lst.scan_errors["seg"]
        assert len(lst.runs) == 4
    finally:
        await svc.close()


async def test_fake_mlflow_orphans_and_mcp(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=ML_INVENTORY)
    await svc.start()
    try:
        lst = await svc.checkpoints.mlflow_orphans()
        names = sorted(o.path.split("/")[-2] for o in lst.runs)
        assert names == ["segformer_b0_debug", "unet_small_2026-08-14"]
        views = {v.id: v for v in svc.checkpoints.sources()}
        assert views["seg"].mlflow_match == "params.callbacks/model_checkpoint/dirpath"
        assert views["flat"].mlflow_match is None
        async with create_connected_server_and_client_session(build_mcp(svc)._mcp_server) as c:
            res = await c.call_tool("list_mlflow_orphans", {"limit": 1})
            assert not res.isError
            data = json.loads(res.content[0].text)  # type: ignore[union-attr]
            assert data["runs_total"] == 2 and len(data["runs"]) == 1
            res = await c.call_tool("list_checkpoints", {"source": "seg"})
            data = json.loads(res.content[0].text)  # type: ignore[union-attr]
            assert any("mlflow_runs" in r for r in data["runs"])
    finally:
        await svc.close()


async def test_http_delete_run() -> None:
    calls: list[tuple[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append((req.url.path, body))
        if body["run_id"] == "gone":
            return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})
        return httpx.Response(200, json={})

    client = HttpMlflow(transport=httpx.MockTransport(handler))
    cfg = MlflowConfig(tracking_uri="http://mlflow.lab:5000")
    try:
        await client.delete_run(cfg, "abc")
        with pytest.raises(MlflowError, match="not found"):
            await client.delete_run(cfg, "gone")
    finally:
        await client.close()
    assert calls[0] == ("/api/2.0/mlflow/runs/delete", {"run_id": "abc"})


async def test_delete_mlflow_orphans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = make_service(tmp_path, text=ML_INVENTORY)
    await svc.start()
    stub = _StubMlflow()
    svc.mlflow.source = stub
    stub.extra = [MlflowHit("/ws/out/gone_b/checkpoints", MlflowRunRef(run_id="y"))]
    try:
        # a run whose directory exists is refused, whatever the others
        with pytest.raises(InvalidRequestError, match="not an MLflow run without directory"):
            await svc.checkpoints.delete_mlflow_orphans(["x", "new"])
        assert not stub.deleted
        stub.delete_error = "MLflow 403: Permission denied"
        res = await svc.checkpoints.delete_mlflow_orphans(["x"])
        assert not res.deleted and res.failed == {"x": "MLflow 403: Permission denied"}
        stub.delete_error = None
        res = await svc.checkpoints.delete_mlflow_orphans(["x", "x"])
        assert res.deleted == ["x"] and not res.failed and res.remaining == 1
        assert stub.deleted == ["x"]
        # gone from the cached searches, without asking MLflow again
        calls = stub.calls
        lst = await svc.checkpoints.mlflow_orphans()
        assert [o.run.run_id for o in lst.runs] == ["y"] and stub.calls == calls

        # a source that cannot be scanned: everything is refused
        real = svc.executor.exec

        async def exec_(machine: Any, argv: list[str], **kw: Any) -> Any:
            if machine.id == "gpu-01":
                raise MachineUnreachableError("gpu-01", "unreachable")
            return await real(machine, argv, **kw)

        monkeypatch.setattr(svc.executor, "exec", exec_)
        svc.checkpoints.clear()
        with pytest.raises(InvalidRequestError, match="could not be scanned"):
            await svc.checkpoints.delete_mlflow_orphans(["y"])
        assert stub.deleted == ["x"]
    finally:
        await svc.close()


def test_api_mlflow_orphans(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=ML_INVENTORY)
    with TestClient(create_app(Settings(), svc)) as client:
        lst = client.get("/api/mlflow-orphans").json()
        assert lst["enabled"] and len(lst["runs"]) == 2
        ids = [r["run"]["run_id"] for r in lst["runs"]]
        bad = client.post("/api/mlflow-orphans/delete", json={"run_ids": ["nope"]})
        assert bad.status_code == 422
        res = client.post("/api/mlflow-orphans/delete", json={"run_ids": ids[:1]}).json()
        assert res["deleted"] == ids[:1] and res["remaining"] == 1
        lst = client.get("/api/mlflow-orphans?refresh=true").json()
        assert [r["run"]["run_id"] for r in lst["runs"]] == ids[1:]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (3.0, "3"),
        (-2, "-2"),
        (0.5, "0.5"),
        (1234567.89, "1.23457e+06"),
        (12345678.0, "12345678"),
        ("NaN", "NaN"),
        (None, None),
        (True, None),
    ],
)
def test_format_metric(value: object, text: str | None) -> None:
    assert format_metric(value) == text
