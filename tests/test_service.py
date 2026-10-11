"""Service layer with the fake executor: full lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from trainhub.core.models import RunSource, RunStatus
from trainhub.executors.fake import FakeExecutor
from trainhub.service import (
    ActionNotAllowedError,
    InvalidRequestError,
    RunService,
    Selection,
    UiState,
)

from .helpers import SEL, make_service, wait_for


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[RunService]:
    s = make_service(tmp_path)
    await s.start()
    yield s
    await s.close()


async def status_is(svc: RunService, run_id: str, *statuses: RunStatus) -> bool:
    return (await svc.get_run(run_id)).status in statuses


async def test_full_lifecycle_success(svc: RunService) -> None:
    events = svc.bus.subscribe()
    run = await svc.launch(SEL, wait=True)
    assert run.status is RunStatus.RUNNING
    assert run.started_at is not None
    assert run.container_name == f"th-segmentation-{run.id}"
    assert run.full_command[0] == "ssh"
    assert "--config-name baseline" in run.command
    chunks = []
    async for chunk in svc.stream_logs(run.id):
        chunks.append(chunk)
    text = b"".join(chunks).decode()
    assert "[HYDRA]" in text and "Epoch 1/" in text and "\r" in text
    await wait_for(lambda: status_is(svc, run.id, RunStatus.SUCCEEDED))
    done = await svc.get_run(run.id)
    assert done.exit_code == 0 and done.finished_at is not None
    types = []
    while not events.queue.empty():
        ev = events.queue.get_nowait()
        assert ev is not None
        if ev.type.startswith("run."):
            types.append(ev.type)
    assert types[0] == "run.created" and "run.updated" in types
    ui = await svc.get_ui_state()
    assert ui.last_selection["segmentation"].config == "baseline"


async def test_pause_freezes_logs_and_resume(svc: RunService) -> None:
    run = await svc.launch(SEL, wait=True)
    sub = await svc.subscribe_logs(run.id)
    await asyncio.sleep(0.3)
    paused = await svc.pause(run.id)
    assert paused.status is RunStatus.PAUSED and paused.paused_at is not None
    await asyncio.sleep(0.1)
    size = len(svc.logs.buffer(run.id) or b"")
    await asyncio.sleep(0.4)
    assert len(svc.logs.buffer(run.id) or b"") == size
    # the monitor does not change the status of a paused run
    await svc.monitor.poll("gpu-01")
    assert (await svc.get_run(run.id)).status is RunStatus.PAUSED
    with pytest.raises(ActionNotAllowedError, match="possible actions: resume, stop"):
        await svc.pause(run.id)
    resumed = await svc.resume(run.id)
    assert resumed.status is RunStatus.RUNNING and resumed.paused_total_s >= 0.4
    await asyncio.sleep(0.3)
    assert len(svc.logs.buffer(run.id) or b"") > size
    svc.unsubscribe_logs(sub)


async def test_stop_running_and_paused(svc: RunService) -> None:
    run = await svc.launch(SEL, wait=True)
    stopped = await svc.stop(run.id, wait=True)
    assert stopped.status is RunStatus.STOPPED and stopped.exit_code == 143
    logs = await svc.read_logs(run.id)
    assert b"SIGTERM" in logs
    await svc.monitor.poll("gpu-01")
    assert (await svc.get_run(run.id)).status is RunStatus.STOPPED

    run2 = await svc.launch(SEL, wait=True)
    await svc.pause(run2.id)
    pending = await svc.stop(run2.id)
    assert pending.status is RunStatus.STOPPING
    with pytest.raises(ActionNotAllowedError):
        await svc.resume(run2.id)
    await wait_for(lambda: status_is(svc, run2.id, RunStatus.STOPPED))
    final = await svc.get_run(run2.id)
    assert final.paused_at is None and final.paused_total_s > 0


async def test_forbidden_transitions(svc: RunService) -> None:
    run = await svc.launch(SEL, wait=True)
    for action in ("resume", "restart", "relaunch"):
        with pytest.raises(ActionNotAllowedError):
            await svc.action(run.id, action)
    with pytest.raises(ActionNotAllowedError, match="delete"):
        await svc.delete(run.id)
    with pytest.raises(InvalidRequestError, match="unknown action"):
        await svc.action(run.id, "explode")


async def test_failure_and_launch_failure(tmp_path: Path) -> None:
    svc = make_service(tmp_path, failure=1.0)
    await svc.start()
    try:
        run = await svc.launch(SEL, wait=True)
        await wait_for(lambda: status_is(svc, run.id, RunStatus.FAILED))
        failed = await svc.get_run(run.id)
        assert failed.exit_code == 1
        assert b"Traceback" in await svc.read_logs(run.id)
    finally:
        await svc.close()


async def test_launch_failure_image_missing(tmp_path: Path) -> None:
    from .helpers import sim_inventory_text

    text = sim_inventory_text().replace(
        "image: registry.lab/ml/torch:2.4-cuda12", "image: registry.lab/ml/missing:1"
    )
    svc = make_service(tmp_path, text=text)
    await svc.start()
    try:
        run = await svc.launch(SEL, wait=True)
        assert run.status is RunStatus.FAILED
        assert run.error and "pull access denied" in run.error
        assert run.finished_at is not None
        await svc.delete(run.id)  # no container: deleting is still possible
    finally:
        await svc.close()


async def test_restart_relaunch_delete(svc: RunService) -> None:
    run = await svc.launch(SEL, wait=True)
    await svc.stop(run.id, wait=True)
    restarted = await svc.restart(run.id)
    assert restarted.status is RunStatus.RUNNING and restarted.exit_code is None
    assert restarted.finished_at is None and not restarted.stop_requested
    await svc.stop(run.id, wait=True)
    new = await svc.relaunch(run.id, source=RunSource.MCP, wait=True)
    assert new.id != run.id and new.source is RunSource.MCP and new.config == run.config
    await svc.delete(run.id)
    assert [r.id for r in await svc.list_runs()] == [new.id]
    fake = svc.executor
    assert isinstance(fake, FakeExecutor)
    assert run.container_name not in fake.machines["gpu-01"].containers


async def test_unreachable_then_back(svc: RunService) -> None:
    await svc.monitor.stop()
    run = await svc.launch(SEL, wait=True)
    fake = svc.executor
    assert isinstance(fake, FakeExecutor)
    fake.forced_state["gpu-01"] = "down"
    await svc.monitor.poll("gpu-01")
    r = await svc.get_run(run.id)
    assert r.status is RunStatus.UNREACHABLE and r.error and "unreachable" in r.error
    assert svc.machine_state("gpu-01").reachable is False
    # the other machines are not affected
    await svc.monitor.poll("gpu-02")
    assert svc.machine_state("gpu-02").reachable is True
    with pytest.raises(ActionNotAllowedError):
        await svc.pause(run.id)
    fake.forced_state["gpu-01"] = "ok"
    await svc.monitor.poll("gpu-01")
    r = await svc.get_run(run.id)
    assert r.status is RunStatus.RUNNING and r.error is None


async def test_action_on_unreachable_machine_gives_clear_error(svc: RunService) -> None:
    from trainhub.service import UnreachableError

    run = await svc.launch(SEL, wait=True)
    fake = svc.executor
    assert isinstance(fake, FakeExecutor)
    fake.forced_state["gpu-01"] = "down"
    with pytest.raises(UnreachableError, match="gpu-01"):
        await svc.pause(run.id)
    fake.forced_state["gpu-01"] = "ok"


async def test_reconciliation_after_restart(tmp_path: Path) -> None:
    fake: FakeExecutor | None = None
    svc1 = make_service(tmp_path)
    fake = svc1.executor
    assert isinstance(fake, FakeExecutor)
    await svc1.start(monitor=False)
    kept = await svc1.launch(SEL, wait=True)
    lost = await svc1.launch(SEL, wait=True)
    # Container launched by another instance (not in the database)
    other = make_service(executor=fake, db=":memory:")
    await other.start(monitor=False)
    orphan = await other.launch(SEL.model_copy(update={"config": "unet_large"}), wait=True)
    await other.store.close()
    # Server stopped, container deleted by hand in the meantime
    await svc1.store.close()
    fake.machines["gpu-01"].containers.pop(lost.container_name)

    svc2 = make_service(tmp_path, executor=fake)
    await svc2.start(monitor=False)
    try:
        await svc2.monitor.poll("gpu-01")
        assert (await svc2.get_run(kept.id)).status is RunStatus.RUNNING
        gone = await svc2.get_run(lost.id)
        assert gone.status is RunStatus.FAILED and gone.error and "not found" in gone.error
        imported = await svc2.get_run(orphan.id)
        assert imported.source is RunSource.IMPORT
        assert imported.config == "unet_large" and imported.project_id == "segmentation"
        assert imported.status is RunStatus.RUNNING
    finally:
        await svc2.close()


async def test_shared_log_stream(svc: RunService) -> None:
    fake = svc.executor
    assert isinstance(fake, FakeExecutor)
    opened = 0
    original = fake.stream

    async def counting(machine, remote):  # type: ignore[no-untyped-def]
        nonlocal opened
        opened += 1
        return await original(machine, remote)

    fake.stream = counting  # type: ignore[method-assign]
    run = await svc.launch(SEL, wait=True)
    a = await svc.subscribe_logs(run.id)
    await asyncio.sleep(0.3)
    b = await svc.subscribe_logs(run.id)
    # the second subscriber first receives the full buffer
    first = await asyncio.wait_for(b.queue.get(), 1)
    assert first.kind == "data" and b"[HYDRA]" in first.data
    assert opened == 1 and svc.logs.subscriber_count(run.id) == 2
    svc.unsubscribe_logs(a)
    svc.unsubscribe_logs(b)
    # an active run stays followed (and saved) without subscribers
    await asyncio.sleep(0.4)  # idle timeout (0.2 s in tests)
    assert svc.logs.is_following(run.id)
    await svc.stop(run.id, wait=True)
    await asyncio.sleep(0.6)  # finish delay + idle timeout
    assert not svc.logs.is_following(run.id)
    assert opened == 1


async def test_buffer_is_bounded(svc: RunService) -> None:
    svc.logs.buffer_size = 1000
    run = await svc.launch(SEL, wait=True)
    sub = await svc.subscribe_logs(run.id)
    await asyncio.sleep(0.5)
    assert len(svc.logs.buffer(run.id) or b"") <= 1000
    svc.unsubscribe_logs(sub)


async def test_configs_static_and_discovered(svc: RunService) -> None:
    cl = await svc.get_configs("segmentation", "gpu-01")
    assert cl.configs == ["baseline", "unet_large"] and cl.discovered is None
    det = await svc.get_configs("detection", "gpu-02")
    assert det.discovered == ["exp/sweep/yolo_l", "yolo_m", "yolo_s"] and det.cached_at is not None
    again = await svc.get_configs("detection", "gpu-02")
    assert again.cached_at == det.cached_at
    assert det.root == "/home/alice/det/configs" and det.run_counts == {}
    sel = Selection(
        project="detection", machine="gpu-02", image="torch-2.4", venv="main",
        config="exp/sweep/yolo_l",
    )  # fmt: skip
    run = await svc.launch(sel, wait=True)
    assert "--config-name exp/sweep/yolo_l" in run.command
    await svc.launch(sel, wait=True)
    await svc.stop(run.id, wait=True)
    counts = (await svc.get_configs("detection", "gpu-02")).run_counts
    assert counts["exp/sweep/yolo_l"].total == 2 and counts["exp/sweep/yolo_l"].active == 1
    assert "yolo_s" not in counts
    with pytest.raises(InvalidRequestError, match="available configs: baseline, unet_large"):
        await svc.launch(SEL.model_copy(update={"config": "foo"}))


async def test_selection_errors(svc: RunService) -> None:
    with pytest.raises(InvalidRequestError, match="machine 'gpu-09'"):
        await svc.preview(SEL.model_copy(update={"machine": "gpu-09"}))
    with pytest.raises(InvalidRequestError, match="available: main"):
        await svc.preview(SEL.model_copy(update={"venv": "exp"}))


async def test_project_command_override(svc: RunService) -> None:
    view = await svc.set_project_command(
        "segmentation", "python train.py --config-name {config} +debug=true"
    )
    assert view.command_overridden
    run = await svc.launch(SEL, wait=True)
    assert run.command.endswith("+debug=true")
    with pytest.raises(InvalidRequestError, match="config"):
        await svc.set_project_command("segmentation", "python train.py")
    view = await svc.set_project_command("segmentation", None)
    assert not view.command_overridden
    assert svc.project_command("segmentation") == "python train.py --config-name {config}"


async def test_ui_state(svc: RunService) -> None:
    state = await svc.get_ui_state()
    assert state.open_tabs == ["segmentation", "detection"]
    saved = await svc.put_ui_state(UiState(open_tabs=["detection", "ghost"], active_tab="ghost"))
    assert saved.open_tabs == ["detection"] and saved.active_tab == "detection"


async def test_preview_and_cuda(svc: RunService) -> None:
    p = await svc.preview(
        Selection(**{**SEL.model_dump(), "cuda_visible_devices": "0,1", "overrides": ["a=1"]})
    )
    assert "CUDA_VISIBLE_DEVICES=0,1" in p.text and p.argv[0] == "ssh"
    assert p.command.endswith("baseline a=1")


async def test_inventory_hot_reload(tmp_path: Path) -> None:
    from trainhub.service import InventoryManager, Store

    from .helpers import sim_inventory_text

    path = tmp_path / "inv.yaml"
    path.write_text(sim_inventory_text())
    mgr = InventoryManager(path)
    svc = RunService(mgr, FakeExecutor(mgr.inventory.simulation), Store(":memory:"))
    await svc.start(monitor=False, watch_inventory=False)
    events = svc.bus.subscribe()
    try:
        import os

        path.write_text(sim_inventory_text().replace("label: Segmentation", "label: Seg 2"))
        os.utime(path, (1, 1))
        assert mgr.check()
        assert svc.inventory.project("segmentation").label == "Seg 2"
        ev = events.queue.get_nowait()
        assert ev is not None and ev.type == "inventory.updated"
        path.write_text("machines: [")
        os.utime(path, (2, 2))
        assert not mgr.check()
        assert mgr.error and "invalid YAML" in mgr.error
        assert svc.inventory.project("segmentation").label == "Seg 2"
    finally:
        await svc.close()


async def test_stop_on_unreachable_machine_can_be_retried(svc: RunService) -> None:
    await svc.monitor.stop()
    run = await svc.launch(SEL, wait=True)
    fake = svc.executor
    assert isinstance(fake, FakeExecutor)
    fake.forced_state["gpu-01"] = "down"
    failed = await svc.stop(run.id, wait=True)
    assert failed.status is RunStatus.UNREACHABLE and not failed.stop_requested
    assert failed.error and "stop failed" in failed.error
    fake.forced_state["gpu-01"] = "ok"
    await svc.monitor.poll("gpu-01")
    assert (await svc.get_run(run.id)).status is RunStatus.RUNNING
    assert (await svc.stop(run.id, wait=True)).status is RunStatus.STOPPED


async def test_container_kept_after_command(svc: RunService) -> None:
    fake = svc.executor
    assert isinstance(fake, FakeExecutor)
    run = await svc.launch(SEL, wait=True)
    assert run.container_alive
    await wait_for(lambda: status_is(svc, run.id, RunStatus.SUCCEEDED))
    done = await svc.get_run(run.id)
    assert done.exit_code == 0 and done.container_alive and done.finished_at is not None
    assert fake.machines["gpu-01"].containers[run.container_name].state == "running"
    logs = await svc.read_logs(run.id)
    assert b"container kept" in logs
    # log following does stop even though the container is still started
    text = b"".join([c async for c in svc.stream_logs(run.id)])
    assert b"Training finished" in text

    shell = await svc.open_shell(run.id)
    banner = await asyncio.wait_for(shell.read(), 1)
    assert b"simul" in banner
    shell.write(b"pwd\r")
    assert b"/workspace" in await asyncio.wait_for(shell.read(), 1)
    await shell.close()

    closed = await svc.close_container(run.id)
    assert closed.status is RunStatus.SUCCEEDED and not closed.container_alive
    c = fake.machines["gpu-01"].containers[run.container_name]
    assert c.state == "exited" and c.exit_code == 0
    await svc.monitor.poll("gpu-01")
    assert (await svc.get_run(run.id)).status is RunStatus.SUCCEEDED
    with pytest.raises(ActionNotAllowedError):
        await svc.close_container(run.id)
    with pytest.raises(ActionNotAllowedError, match="not started"):
        await svc.open_shell(run.id)


async def test_failed_command_keeps_container_and_restart(tmp_path: Path) -> None:
    svc = make_service(tmp_path, failure=1.0)
    await svc.start()
    try:
        run = await svc.launch(SEL, wait=True)
        await wait_for(lambda: status_is(svc, run.id, RunStatus.FAILED))
        failed = await svc.get_run(run.id)
        assert failed.exit_code == 1 and failed.container_alive
        # docker restart on a kept container: the command starts over
        again = await svc.restart(run.id)
        assert again.status is RunStatus.RUNNING and again.container_alive
        await wait_for(lambda: status_is(svc, run.id, RunStatus.FAILED))
        # deleting a container that is still started: docker rm -f
        await svc.delete(run.id)
        fake = svc.executor
        assert isinstance(fake, FakeExecutor)
        assert run.container_name not in fake.machines["gpu-01"].containers
    finally:
        await svc.close()


async def test_logs_saved_to_file_and_kept(tmp_path: Path) -> None:
    svc1 = make_service(tmp_path)
    fake = svc1.executor
    assert isinstance(fake, FakeExecutor)
    await svc1.start()
    run = await svc1.launch(SEL, wait=True)
    path = svc1.log_path(run.id)
    assert path is not None
    # saved as it comes, without subscribers
    await wait_for(lambda: _async(path.exists() and b"[HYDRA]" in path.read_bytes()))
    await wait_for(lambda: status_is(svc1, run.id, RunStatus.SUCCEEDED))
    await wait_for(lambda: _async(path.with_suffix(".done").exists()))
    saved = path.read_bytes()
    assert b"Training finished" in saved and b"container kept" in saved
    await svc1.close()

    # container deleted outside TrainHub, then TrainHub restarts
    fake.machines["gpu-01"].containers.pop(run.container_name)
    svc2 = make_service(tmp_path, executor=fake)
    await svc2.start(monitor=False)
    try:
        await svc2.monitor.poll("gpu-01")
        sub = await svc2.subscribe_logs(run.id)
        first = await asyncio.wait_for(sub.queue.get(), 1)
        assert first.kind == "data" and first.data == saved
        assert (await asyncio.wait_for(sub.queue.get(), 1)).kind == "end"
        svc2.unsubscribe_logs(sub)
        assert await svc2.read_logs(run.id) == saved
        assert (await svc2.read_logs(run.id, tail=1)).startswith(b"[trainhub]")
        # deleting the run keeps the backup file
        await svc2.delete(run.id)
        assert path.read_bytes() == saved
    finally:
        await svc2.close()


async def test_log_file_not_truncated_by_shorter_session(tmp_path: Path) -> None:
    from trainhub.service.log_hub import _LogFile

    f = _LogFile(tmp_path / "r1.log")
    f.begin()
    f.write(b"0123456789")
    f.close(final=False)
    # session interrupted before catching up with the old file: abandoned
    f.begin()
    f.write(b"abc")
    f.close(final=False)
    assert f.path.read_bytes() == b"0123456789" and not f.part.exists()
    # session ended shorter (Docker logs truncated): the old file is kept aside
    f.begin()
    f.write(b"xyz")
    f.close(final=True)
    assert f.path.read_bytes() == b"xyz" and f.prev.read_bytes() == b"0123456789"
    assert f.complete
    # session catching up with the old file: replaces it as it goes
    f.begin()
    assert not f.complete
    f.write(b"xyz+more")
    assert f.path.read_bytes() == b"xyz+more"
    f.close(final=False)


async def test_store_migrates_old_schema(tmp_path: Path) -> None:
    import aiosqlite

    from trainhub.service import Store
    from trainhub.service.store import _SCHEMA

    db = tmp_path / "old.sqlite"
    async with aiosqlite.connect(db) as conn:
        old_schema = _SCHEMA.replace(",\n    container_alive INTEGER NOT NULL DEFAULT 0", "")
        assert "container_alive" not in old_schema
        await conn.executescript(old_schema)
        await conn.commit()
    store = Store(db)
    await store.open()
    async with store.db.execute("PRAGMA table_info(runs)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    await store.close()
    assert "container_alive" in cols


async def _async(value: bool) -> bool:
    return value


async def test_params_saved_remembered_and_relaunched(svc: RunService) -> None:
    await svc.set_project_command(
        "segmentation", "python train.py --config-name {config} {options} --tag {tag}"
    )
    assert svc.project_view(svc.inventory.project("segmentation")).params == ["options", "tag"]
    sel = SEL.model_copy(update={"venv": "system", "params": {"options": "a=1 b=2", "tag": "x"}})
    run = await svc.launch(sel, wait=True)
    assert run.params == {"options": "a=1 b=2", "tag": "x"}
    assert run.command == "python train.py --config-name baseline a=1 b=2 --tag x"
    assert "source" not in run.full_command[-1]
    ui = await svc.get_ui_state()
    assert ui.last_selection["segmentation"].params == {"options": "a=1 b=2", "tag": "x"}
    await svc.stop(run.id, wait=True)
    again = await svc.relaunch(run.id, wait=True)
    assert again.params == run.params and again.venv_id == "system"
    with pytest.raises(InvalidRequestError, match="tag"):
        await svc.preview(SEL.model_copy(update={"params": {"tag": "'unclosed"}}))
