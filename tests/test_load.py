"""Machine load: /proc parsing, fake simulation, service and API."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from trainhub.core.load import CPU_SCRIPT, parse_cpu, query_cpu

from .helpers import SEL, make_service, wait_for

PROC = """\
3.50 2.10 1.00 4/900 12345
---
cpu  100 0 100 800 0 0 0 0 0 0
cpu0 50 0 50 400 0 0 0 0 0 0
cpu1 50 0 50 400 0 0 0 0 0 0
---
cpu  200 0 150 850 0 0 0 0 0 0
cpu0 140 0 60 400 0 0 0 0 0 0
cpu1 60 0 90 450 0 0 0 0 0 0
---
MemTotal:       16384000 kB
MemAvailable:    4096000 kB
"""


def test_parse_cpu() -> None:
    c = parse_cpu(PROC)
    assert c.cores == 2
    assert c.per_core_pct == [100.0, 50.0]
    assert c.usage_pct == 75.0
    assert c.load_avg == (3.5, 2.1, 1.0)
    assert c.memory_total_mib == 16000 and c.memory_used_mib == 12000


def test_parse_cpu_errors() -> None:
    with pytest.raises(ValueError):
        parse_cpu("")
    with pytest.raises(ValueError):
        parse_cpu("1 2 3\n---\nnothing\n---\nnothing\n")


@pytest.mark.skipif(not Path("/proc/stat").exists(), reason="Linux /proc only")
def test_real_proc() -> None:
    out = subprocess.run(query_cpu(), capture_output=True, text=True, check=True).stdout
    c = parse_cpu(out)
    assert c.cores >= 1 and len(c.per_core_pct) == c.cores
    assert 0 <= c.usage_pct <= 100
    assert c.memory_total_mib and c.memory_used_mib is not None


async def test_service_machine_load(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    await svc.start(monitor=False, watch_inventory=False)
    try:
        idle = await svc.machine_load("gpu-01")
        assert idle.reachable and idle.cpu is not None and idle.cpu.cores == 64
        assert len(idle.gpus) == 4 and idle.cpu_error is None and idle.gpu_error is None
        run = await svc.launch(SEL.model_copy(update={"cuda_visible_devices": "1"}), wait=True)

        async def busy() -> bool:
            ld = await svc.machine_load("gpu-01")
            return ld.cpu is not None and ld.cpu.usage_pct > idle.cpu.usage_pct + 5  # type: ignore[union-attr]

        await wait_for(busy)
        ld = await svc.machine_load("gpu-01")
        assert [r.id for r in ld.active_runs] == [run.id]
        assert ld.gpus[1].runs == [run.id]
        svc.executor.forced_state["gpu-02"] = "down"  # type: ignore[attr-defined]
        down = await svc.machine_load("gpu-02")
        assert not down.reachable and down.error and down.cpu is None
    finally:
        await svc.close()


async def test_load_without_proc(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    await svc.start(monitor=False, watch_inventory=False)
    try:

        async def failing(*args: object, **kwargs: object) -> object:
            from trainhub.executors.base import ExecResult

            return ExecResult(argv=[], returncode=1, stderr=b"sh: /proc: not found")

        svc.executor._exec = failing  # type: ignore[method-assign,assignment]
        ld = await svc.machine_load("gpu-01")
        assert ld.reachable and ld.cpu is None and ld.cpu_error and "not found" in ld.cpu_error
        assert ld.gpu_error and ld.gpus == []
    finally:
        await svc.close()


def test_script_is_posix_sh() -> None:
    if sys.platform != "linux":
        pytest.skip("Linux only")
    subprocess.run(["sh", "-n", "-c", CPU_SCRIPT], check=True)
