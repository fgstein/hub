"""Couche service avec l'exécuteur fake : cycle de vie complet."""

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
    # le monitor ne change pas le statut d'un run en pause
    await svc.monitor.poll("gpu-01")
    assert (await svc.get_run(run.id)).status is RunStatus.PAUSED
    with pytest.raises(ActionNotAllowedError, match="actions possibles : resume, stop"):
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
    with pytest.raises(ActionNotAllowedError, match="supprimer"):
        await svc.delete(run.id)
    with pytest.raises(InvalidRequestError, match="action inconnue"):
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
        await svc.delete(run.id)  # aucun conteneur : suppression quand même possible
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
    assert r.status is RunStatus.UNREACHABLE and r.error and "injoignable" in r.error
    assert svc.machine_state("gpu-01").reachable is False
    # les autres machines ne sont pas affectées
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
    # Conteneur lancé par une autre instance (absent de la base)
    other = make_service(executor=fake, db=":memory:")
    await other.start(monitor=False)
    orphan = await other.launch(SEL.model_copy(update={"config": "unet_large"}), wait=True)
    await other.store.close()
    # Arrêt du serveur, conteneur supprimé à la main entre-temps
    await svc1.store.close()
    fake.machines["gpu-01"].containers.pop(lost.container_name)

    svc2 = make_service(tmp_path, executor=fake)
    await svc2.start(monitor=False)
    try:
        await svc2.monitor.poll("gpu-01")
        assert (await svc2.get_run(kept.id)).status is RunStatus.RUNNING
        gone = await svc2.get_run(lost.id)
        assert gone.status is RunStatus.FAILED and gone.error and "introuvable" in gone.error
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
    # le second abonné reçoit d'abord le tampon complet
    first = await asyncio.wait_for(b.queue.get(), 1)
    assert first.kind == "data" and b"[HYDRA]" in first.data
    assert opened == 1 and svc.logs.subscriber_count(run.id) == 2
    svc.unsubscribe_logs(a)
    svc.unsubscribe_logs(b)
    assert svc.logs.is_following(run.id)
    await asyncio.sleep(0.4)  # délai d'inactivité (0.2 s en test)
    assert not svc.logs.is_following(run.id)


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
    assert det.discovered == ["yolo_m", "yolo_s"] and det.cached_at is not None
    again = await svc.get_configs("detection", "gpu-02")
    assert again.cached_at == det.cached_at
    with pytest.raises(InvalidRequestError, match="configs disponibles : baseline, unet_large"):
        await svc.launch(SEL.model_copy(update={"config": "foo"}))


async def test_selection_errors(svc: RunService) -> None:
    with pytest.raises(InvalidRequestError, match="machine 'gpu-09'"):
        await svc.preview(SEL.model_copy(update={"machine": "gpu-09"}))
    with pytest.raises(InvalidRequestError, match="disponibles : main"):
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
        assert mgr.error and "YAML invalide" in mgr.error
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
    assert failed.error and "arrêt impossible" in failed.error
    fake.forced_state["gpu-01"] = "ok"
    await svc.monitor.poll("gpu-01")
    assert (await svc.get_run(run.id)).status is RunStatus.RUNNING
    assert (await svc.stop(run.id, wait=True)).status is RunStatus.STOPPED
