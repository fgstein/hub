"""Checkpoint sources: layout, find listing, deletion script (real shell), service, API, MCP."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from trainhub.api.app import create_app
from trainhub.config import Settings
from trainhub.core import checkpoints as ck
from trainhub.core.inventory import InventoryError, parse_inventory
from trainhub.core.models import CheckpointSource, RunStatus
from trainhub.mcp.server import build_mcp
from trainhub.service import InvalidRequestError, NotFoundError, RunService, Selection
from trainhub.service.checkpoints import build_listing

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
    counts = {"/out/run_new/checkpoints/last/epoch0000003.ckpt": {"total": 2, "active": 1}}
    lst = build_listing(_src(), entries, counts)
    assert lst.size == 300 + 100 + 50 + 7 + 5 and lst.count == 5
    assert [r.name for r in lst.runs] == ["empty_run", "run_new", "run_old"]
    new = lst.runs[1]
    assert new.size == 457 and new.count == 4
    assert [g.name for g in new.groups] == ["empty", "last", "topk"]
    topk = new.groups[2]
    assert [f.name for f in topk.files] == ["epoch0000001.ckpt", "epoch0000002.ckpt"]
    assert topk.size == 400 and topk.path == "run_new/checkpoints/topk"
    last = new.groups[1].files[0]
    assert last.runs is not None and last.runs.total == 2
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
    lst = build_listing(src, ck.parse_listing(res.stdout), {})
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


async def test_inference_with_checkpoint(svc: RunService) -> None:
    lst = await svc.checkpoints.listing("seg")
    f = lst.runs[0].groups[1].files[0]
    host = f"{ROOT}/{f.path}"
    with pytest.raises(InvalidRequestError, match="takes a"):
        await svc.preview(INFER)
    with pytest.raises(InvalidRequestError, match="no \\{checkpoint\\}"):
        await svc.preview(INFER.model_copy(update={"project": "segmentation", "checkpoint": host}))
    with pytest.raises(InvalidRequestError, match="not mounted"):
        await svc.preview(INFER.model_copy(update={"checkpoint": "/elsewhere/e.ckpt"}))
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
    lst = await svc.checkpoints.listing("seg")
    again = lst.runs[0].groups[1].files[0]
    assert again.runs is not None and again.runs.total == 1
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
