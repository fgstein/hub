"""Series: the same run repeated several times in a row, in one container."""

from __future__ import annotations

import asyncio
import json
import random
import signal
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from trainhub.cli import _series as cli_series
from trainhub.cli import build_parser
from trainhub.core import commands as c
from trainhub.core.inventory import parse_inventory
from trainhub.core.models import Inventory, Run, Series
from trainhub.core.selection import SelectionError, make_spec
from trainhub.core.series import SeriesTracker, draw_values, end_marker, start_marker
from trainhub.core.status import utcnow
from trainhub.service import InvalidRequestError, RunService, Selection, Store
from trainhub.service.schemas import RandomRange, SeriesRequest

from .helpers import make_service, wait_for

SCRIPT_INVENTORY = """
machines: [{id: m, host: localhost}]
images: [{id: i, image: x}]
mlflow: {tracking_uri: "http://mlflow:5000"}
projects:
  - id: p
    command: >-
      sh -c 'echo "cfg=$1 seed=$2 idx=$TRAINHUB_SERIES_INDEX ctx=$MLFLOW_RUN_CONTEXT"; exit $3'
      -- {config} {seed} {code}
    docker: {keep_alive: %s}
"""


def _series(values: list[dict[str, str]], *, keep_going: bool = False) -> Series:
    return Series(count=len(values), continue_on_failure=keep_going, values=values)


def _script(tmp_path: Path, series: Series, *, keep_alive: bool = True) -> tuple[str, Path]:
    inv = parse_inventory(SCRIPT_INVENTORY % str(keep_alive).lower(), "t.yaml")
    spec = make_spec(
        inv,
        run_id="r1",
        project="p",
        machine="m",
        image="i",
        venv="system",
        config="base",
        params={"code": "0"},
        series=series,
    )
    exit_file = tmp_path / "exit"
    return c.inner_script(spec).replace(c.EXIT_FILE, str(exit_file)), exit_file


def _run_until(script: str, needle: str) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    seen = ""
    for line in proc.stdout:
        seen += line
        if needle in line:
            return proc, seen
    proc.kill()
    raise AssertionError(f"{needle!r} missing from the output: {seen!r}")


# -- drawing and markers ---------------------------------------------------------------


def test_draw_values_distinct_within_range() -> None:
    values = draw_values({"seed": (0, 9), "other": (5, 5)}, 10, random.Random(1))
    assert len(values) == 10
    assert sorted(int(v["seed"]) for v in values) == list(range(10))
    # range smaller than the count: values repeat
    assert {v["other"] for v in values} == {"5"}


def test_draw_values_default_range_is_large() -> None:
    values = draw_values({"seed": (0, 2**31 - 1)}, 50)
    assert len({v["seed"] for v in values}) == 50


def test_tracker_reads_markers_split_between_chunks() -> None:
    t = SeriesTracker()
    text = (f"{start_marker(1, 3)} · seed=4\r\nEpoch 1\r{end_marker(1, 3, 0)[:12]}").encode()
    assert t.feed(text)
    assert (t.current, t.exit_codes) == (1, [])
    assert t.feed(f"{end_marker(1, 3, 0)[12:]}\n{start_marker(2, 3)}\n".encode())
    assert (t.current, t.exit_codes) == (2, [0])
    assert not t.feed(b"progress bar\r")
    assert t.feed(f"\n{end_marker(2, 3, 1)}\n".encode())
    assert (t.current, t.exit_codes) == (2, [0, 1])


def test_series_request_validation() -> None:
    with pytest.raises(ValidationError):
        SeriesRequest(count=0)
    with pytest.raises(ValidationError):
        SeriesRequest(count=1000)
    with pytest.raises(ValidationError, match="empty range"):
        SeriesRequest(count=2, random={"seed": RandomRange(min=5, max=1)})
    with pytest.raises(ValidationError, match="2 set"):
        SeriesRequest(count=3, values=[{"seed": "1"}, {"seed": "2"}])
    plan = SeriesRequest(count=3, random={"seed": RandomRange(min=0, max=2)}).plan()
    assert sorted(v["seed"] for v in plan.values) == ["0", "1", "2"]
    assert plan.random == {"seed": (0, 2)}


def test_make_spec_rejects_unknown_series_param(inventory: Inventory) -> None:
    with pytest.raises(SelectionError, match="series: parameter"):
        make_spec(
            inventory,
            run_id="r1",
            project="segmentation",
            machine="gpu-01",
            image="torch-2.4",
            venv="main",
            config="baseline",
            series=_series([{"seed": "1"}]),
        )


def test_labels_and_resolved_command(tmp_path: Path) -> None:
    inv = parse_inventory(SCRIPT_INVENTORY % "true", "t.yaml")
    spec = make_spec(
        inv,
        run_id="r1",
        project="p",
        machine="m",
        image="i",
        venv="system",
        config="base",
        series=_series([{"seed": "11"}, {"seed": "22"}], keep_going=True),
    )
    argv = c.docker_run(spec)
    assert f"{c.LABEL_SERIES}=2" in argv and f"{c.LABEL_SERIES_CONTINUE}=1" in argv
    assert "11" in c.resolved_command(spec) and "22" in c.resolved_command(spec, 2)


# -- the script, in a real bash ------------------------------------------------------------


def test_script_runs_every_repetition_with_its_values(tmp_path: Path) -> None:
    series = _series([{"seed": "1"}, {"seed": "'two words'"}, {"seed": "3"}])
    script, exit_file = _script(tmp_path, series)
    proc, out = _run_until(script, "[trainhub] command finished")
    try:
        assert "cfg=base seed=1 idx=1" in out
        assert "cfg=base seed=two words idx=2" in out  # quoted: a single argument
        assert end_marker(3, 3, 0) in out
        assert exit_file.read_text().strip() == "0"
        # MLflow tags of each repetition: the run id and the index
        ctx = out.split("idx=2 ctx=")[1].splitlines()[0]
        assert json.loads(ctx) == {"trainhub.run_id": "r1", "trainhub.series_index": "2"}
        proc.send_signal(signal.SIGTERM)  # closing the kept container
        assert proc.wait(5) == 0
    finally:
        proc.kill()


@pytest.mark.parametrize("keep_going", [False, True])
def test_script_failure_stops_or_goes_on(tmp_path: Path, keep_going: bool) -> None:
    values = [{"seed": "1", "code": code} for code in ("0", "3", "0")]
    series = _series(values, keep_going=keep_going)
    script, _ = _script(tmp_path, series, keep_alive=False)
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert res.returncode == 3  # code of the failure
    assert end_marker(2, 3, 3) in res.stdout
    assert (start_marker(3, 3) in res.stdout) is keep_going
    assert ("series stopped: run 2/3 failed" in res.stdout) is not keep_going


def test_script_stop_ends_the_series(tmp_path: Path) -> None:
    inv = parse_inventory(
        """
machines: [{id: m, host: localhost}]
images: [{id: i, image: x}]
projects:
  - id: p
    command: sh -c 'echo ready; sleep $0' {seed} {config}
""",
        "t.yaml",
    )
    spec = make_spec(
        inv,
        run_id="r1",
        project="p",
        machine="m",
        image="i",
        venv="system",
        config="0",
        series=_series([{"seed": "30"}, {"seed": "30"}]),
    )
    script = c.inner_script(spec).replace(c.EXIT_FILE, str(tmp_path / "exit"))
    proc, _ = _run_until(script, "ready")
    try:
        proc.send_signal(signal.SIGTERM)  # docker stop
        assert proc.wait(5) == 143
        assert proc.stdout is not None
        assert start_marker(2, 2) not in proc.stdout.read()
        assert not (tmp_path / "exit").exists()
    finally:
        proc.kill()


# -- service, with the fake executor ----------------------------------------------------

SERIES_SEL = Selection(
    project="detection",
    machine="gpu-01",
    image="torch-2.4",
    venv="main",
    config="yolo_s",
    series=SeriesRequest(count=3, random={}),
)


DETECTION_COMMAND = "python -m det.train --config-name {config} seed={seed}"


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[RunService]:
    s = make_service(tmp_path, duration=4, scale=40)
    await s.start()
    await s.set_project_command(
        "detection", "python -m det.train --config-name {config} seed={seed}"
    )
    yield s
    await s.close()


async def _finished(svc: RunService, run_id: str) -> Run:
    """The run once finished and its logs (hence its series progress) read to the end."""

    async def done() -> bool:
        return (await svc.get_run(run_id)).status.is_finished

    async def logs_read() -> bool:
        return not svc.logs.is_following(run_id)

    await wait_for(done, timeout=20)
    await wait_for(logs_read)
    await asyncio.sleep(0.05)  # progress updates spawned by the last chunks
    return await svc.get_run(run_id)


async def test_series_runs_every_repetition(svc: RunService) -> None:
    sel = SERIES_SEL.model_copy(
        update={"series": SeriesRequest(count=3, random={"seed": RandomRange(min=0, max=999)})}
    )
    run = await svc.launch(sel, wait=True)
    assert run.series is not None and run.series.count == 3
    seeds = [v["seed"] for v in run.series.values]
    assert len(set(seeds)) == 3 and all(0 <= int(s) <= 999 for s in seeds)
    assert f"seed={seeds[0]}" in run.command
    run = await _finished(svc, run.id)
    assert run.status.value == "succeeded" and run.exit_code == 0
    assert run.series is not None
    assert run.series.current == 3 and run.series.exit_codes == [0, 0, 0]
    # a single container, a single run
    assert len(await svc.list_runs()) == 1
    logs = (await svc.read_logs(run.id)).decode()
    assert logs.count("[trainhub] series run") == 6


async def test_series_stops_at_first_failure(tmp_path: Path) -> None:
    svc = make_service(tmp_path, duration=4, scale=40, failure=1.0)
    await svc.start()
    await svc.set_project_command("detection", DETECTION_COMMAND)
    try:
        run = await svc.launch(SERIES_SEL, wait=True)
        run = await _finished(svc, run.id)
        assert run.status.value == "failed" and run.exit_code == 1
        assert run.series is not None
        assert run.series.exit_codes == [1] and run.series.current == 1
        assert "series stopped: run 1/3 failed" in (await svc.read_logs(run.id)).decode()

        go_on = SERIES_SEL.model_copy(
            update={"series": SeriesRequest(count=2, continue_on_failure=True)}
        )
        run = await _finished(svc, (await svc.launch(go_on, wait=True)).id)
        assert run.series is not None and run.series.exit_codes == [1, 1]
    finally:
        await svc.close()


async def test_series_relaunch_reproduces_values_and_restart_resets(svc: RunService) -> None:
    sel = SERIES_SEL.model_copy(
        update={"series": SeriesRequest(count=2, random={"seed": RandomRange()})}
    )
    first = await _finished(svc, (await svc.launch(sel, wait=True)).id)
    again = await svc.relaunch(first.id, wait=True)
    assert again.id != first.id and again.series is not None and first.series is not None
    assert again.series.values == first.series.values
    assert again.series.current == 0
    await _finished(svc, again.id)

    restarted = await svc.restart(first.id)
    assert restarted.series is not None
    assert restarted.series.current == 0 and restarted.series.exit_codes == []
    done = await _finished(svc, first.id)
    assert done.series is not None and done.series.exit_codes == [0, 0]


async def test_series_unknown_random_param(svc: RunService) -> None:
    sel = SERIES_SEL.model_copy(
        update={"series": SeriesRequest(count=2, random={"nope": RandomRange()})}
    )
    with pytest.raises(InvalidRequestError, match="series: parameter"):
        await svc.launch(sel)


async def test_series_preview(svc: RunService) -> None:
    sel = SERIES_SEL.model_copy(
        update={"series": SeriesRequest(count=2, random={"seed": RandomRange(min=7, max=7)})}
    )
    p = await svc.preview(sel)
    assert "seed=7" in p.command and "series run 2/2" in p.exact


async def test_store_roundtrip(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    await store.open()
    run = Run(
        id="r1",
        project_id="p",
        machine_id="m",
        image_id="i",
        venv_id="v",
        config="c",
        command="x",
        container_name="th-p-r1",
        status="running",  # type: ignore[arg-type]
        created_at=utcnow(),
        series=Series(count=2, values=[{"seed": "1"}, {"seed": "2"}], current=1),
    )
    await store.save_run(run)
    assert await store.get_run("r1") == run
    await store.save_run(run.model_copy(update={"id": "r2", "series": None}))
    loaded = await store.get_run("r2")
    assert loaded is not None and loaded.series is None
    await store.close()


def test_cli_series_arguments() -> None:
    parser = build_parser()
    base = ["launch", "-p", "p", "-m", "m", "-i", "i", "-e", "e", "-c", "c"]
    args = parser.parse_args([*base, "-n", "4", "-R", "seed", "-R", "fold=0:4"])
    req = cli_series(args)
    assert req is not None and req.count == 4 and not req.continue_on_failure
    assert req.random["seed"] == RandomRange()
    assert req.random["fold"] == RandomRange(min=0, max=4)
    assert cli_series(parser.parse_args(base)) is None
    with pytest.raises(SystemExit):
        cli_series(parser.parse_args([*base, "-n", "2", "-R", "seed=3"]))
