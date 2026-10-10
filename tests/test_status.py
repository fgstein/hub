from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from trainhub.core.models import Run, RunStatus
from trainhub.core.status import (
    elapsed_seconds,
    format_duration,
    parse_docker_time,
    parse_inspect,
    parse_ps,
    paused_seconds,
    status_from_container,
)

PS_OUTPUT = (
    "\n".join(
        json.dumps(d)
        for d in [
            {
                "Command": '"bash -lc …"',
                "CreatedAt": "2026-10-10 12:00:01 +0200 CEST",
                "ID": "abc123",
                "Image": "registry.lab/ml/torch:2.4-cuda12",
                "Labels": "trainhub=1,trainhub.run_id=r7f3k2,trainhub.project=segmentation",
                "Names": "th-segmentation-r7f3k2",
                "State": "running",
                "Status": "Up 2 hours",
            },
            {
                "ID": "def456",
                "Image": "img",
                "Labels": "trainhub=1,trainhub.run_id=r00001",
                "Names": "th-seg-r00001",
                "State": "exited",
                "Status": "Exited (137) 3 minutes ago",
            },
            {
                "ID": "x",
                "Labels": "trainhub=1",
                "Names": "th-seg-r00002",
                "Status": "Up 5 minutes (Paused)",
            },
        ]
    )
    + "\nnot json\n"
)


def test_parse_ps() -> None:
    states = parse_ps(PS_OUTPUT)
    assert [s.name for s in states] == [
        "th-segmentation-r7f3k2",
        "th-seg-r00001",
        "th-seg-r00002",
    ]
    assert states[0].state == "running" and states[0].run_id == "r7f3k2"
    assert states[0].created_at == datetime(2026, 10, 10, 10, 0, 1, tzinfo=UTC)
    assert states[1].exit_code == 137
    assert states[2].state == "paused"


INSPECT = json.dumps(
    [
        {
            "Id": "abc",
            "Name": "/th-seg-r1",
            "Created": "2026-10-10T10:00:00.123456789Z",
            "State": {
                "Status": "exited",
                "Running": False,
                "Paused": False,
                "ExitCode": 1,
                "StartedAt": "2026-10-10T10:00:01.5Z",
                "FinishedAt": "2026-10-10T11:00:01Z",
            },
            "Config": {"Image": "img", "Labels": {"trainhub": "1", "trainhub.run_id": "r1"}},
        }
    ]
)


def test_parse_inspect() -> None:
    st = parse_inspect(INSPECT)
    assert st is not None
    assert st.name == "th-seg-r1" and st.exit_code == 1 and st.state == "exited"
    assert st.started_at == datetime(2026, 10, 10, 10, 0, 1, 500000, tzinfo=UTC)
    assert st.finished_at == datetime(2026, 10, 10, 11, 0, 1, tzinfo=UTC)
    assert parse_inspect("[]") is None
    assert parse_inspect("garbage") is None


def test_zero_time_is_none() -> None:
    assert parse_docker_time("0001-01-01T00:00:00Z") is None


@pytest.mark.parametrize(
    ("state", "code", "stop", "expected"),
    [
        ("running", None, False, RunStatus.RUNNING),
        ("running", None, True, RunStatus.STOPPING),
        ("paused", None, False, RunStatus.PAUSED),
        ("created", None, False, RunStatus.LAUNCHING),
        ("exited", 0, False, RunStatus.SUCCEEDED),
        ("exited", 1, False, RunStatus.FAILED),
        ("exited", 143, True, RunStatus.STOPPED),
        ("dead", None, False, RunStatus.FAILED),
    ],
)
def test_status_mapping(state: str, code: int | None, stop: bool, expected: RunStatus) -> None:
    from trainhub.core.status import ContainerState

    c = ContainerState(name="x", state=state, exit_code=code)
    assert status_from_container(c, stop_requested=stop) == expected


@pytest.mark.parametrize(
    ("secs", "text"),
    [
        (0, "0s"),
        (12, "12s"),
        (59.9, "59s"),
        (60, "1m 00s"),
        (245, "4m 05s"),
        (3600, "1h 00m"),
        (3 * 3600 + 12 * 60 + 59, "3h 12m"),
        (2 * 86400 + 4 * 3600 + 5, "2j 04h"),
        (-5, "0s"),
    ],
)
def test_format_duration(secs: float, text: str) -> None:
    assert format_duration(secs) == text


def _run(**kw: object) -> Run:
    base: dict[str, object] = dict(
        id="r1",
        project_id="p",
        machine_id="m",
        image_id="i",
        venv_id="v",
        config="c",
        command="x",
        container_name="th-p-r1",
        status=RunStatus.RUNNING,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    base.update(kw)
    return Run.model_validate(base)


def test_elapsed() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    now = t0 + timedelta(minutes=10)
    assert elapsed_seconds(_run(), now) == 0
    assert elapsed_seconds(_run(started_at=t0), now) == 600
    finished = _run(started_at=t0, finished_at=t0 + timedelta(minutes=3))
    assert elapsed_seconds(finished, now) == 180
    paused = _run(started_at=t0, paused_total_s=30, paused_at=t0 + timedelta(minutes=9))
    assert paused_seconds(paused, now) == 90
