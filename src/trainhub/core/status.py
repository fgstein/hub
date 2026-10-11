"""Parsing ``docker ps`` / ``docker inspect`` output and computing statuses and durations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

from .commands import LABEL_ROOT
from .models import Run, RunStatus

_EXIT_RE = re.compile(r"Exited \((-?\d+)\)")
_HEALTH_RE = re.compile(r"\((healthy|unhealthy|health: starting)\)")


@dataclass(frozen=True)
class ContainerState:
    """State of a container as reported by Docker."""

    name: str
    state: str
    """``created``, ``running``, ``paused``, ``restarting``, ``exited``, ``dead``, ``removing``."""
    exit_code: int | None = None
    labels: dict[str, str] = field(default_factory=dict)
    image: str = ""
    id: str = ""
    status_text: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    health: str | None = None
    """``starting``, ``healthy`` or ``unhealthy`` (end-of-command healthcheck)."""

    @property
    def alive(self) -> bool:
        """Container started (including paused)."""
        return self.state in ("running", "paused", "restarting")

    @property
    def command_done(self) -> bool:
        """Kept container whose command has finished (``unhealthy`` healthcheck)."""
        return self.alive and self.health == "unhealthy"

    @property
    def run_id(self) -> str | None:
        return self.labels.get("trainhub.run_id")

    @property
    def is_trainhub(self) -> bool:
        return self.labels.get(LABEL_ROOT) == "1"


def parse_labels(text: str) -> dict[str, str]:
    """Parse ``a=b,c=d`` (``docker ps`` format)."""
    out: dict[str, str] = {}
    if not text:
        return out
    for part in text.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v
        elif part.strip() and out:
            # comma inside a value: glue it back to the previous one
            last = next(reversed(out))
            out[last] += "," + part
    return out


def parse_docker_time(value: str | None) -> datetime | None:
    """Parse Docker timestamps (RFC 3339 nanoseconds or ``2024-01-01 10:00:00 +0000 UTC``)."""
    if not value or value.startswith("0001-01-01"):
        return None
    v = value.strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?", v)
    if not m:
        return None
    date, time, frac, tz = m.groups()
    micro = int((frac or ".0")[1:7].ljust(6, "0"))
    dt = datetime.fromisoformat(f"{date}T{time}").replace(microsecond=micro)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        return dt.replace(tzinfo=timezone(sign * offset)).astimezone(UTC)
    return dt.replace(tzinfo=UTC)


def parse_ps(output: str) -> list[ContainerState]:
    """Parse the output of ``docker ps -a --format '{{json .}}'`` (one JSON line per container)."""
    states: list[ContainerState] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        status_text = str(d.get("Status", ""))
        state = str(d.get("State", "")).lower()
        if not state:
            state = _state_from_status(status_text)
        m = _EXIT_RE.search(status_text)
        h = _HEALTH_RE.search(status_text)
        names = str(d.get("Names", "")).split(",")[0].lstrip("/")
        states.append(
            ContainerState(
                name=names,
                state=state,
                exit_code=int(m.group(1)) if m else None,
                labels=parse_labels(str(d.get("Labels", ""))),
                image=str(d.get("Image", "")),
                id=str(d.get("ID", "")),
                status_text=status_text,
                created_at=parse_docker_time(str(d.get("CreatedAt", ""))),
                health=h.group(1).removeprefix("health: ") if h else None,
            )
        )
    return states


def _state_from_status(status: str) -> str:
    s = status.lower()
    if "paused" in s:
        return "paused"
    if s.startswith("up"):
        return "running"
    if s.startswith("exited"):
        return "exited"
    if s.startswith("created"):
        return "created"
    if s.startswith("restarting"):
        return "restarting"
    if "dead" in s:
        return "dead"
    return s or "unknown"


def parse_inspect(output: str) -> ContainerState | None:
    """Parse the JSON output of ``docker inspect`` (first container)."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]
    if not isinstance(data, dict):
        return None
    st = data.get("State") or {}
    cfg = data.get("Config") or {}
    if st.get("Paused"):
        state = "paused"
    else:
        state = str(st.get("Status", "")).lower() or ("running" if st.get("Running") else "exited")
    exit_code = st.get("ExitCode")
    code: int | None = (
        int(exit_code) if exit_code is not None and state in ("exited", "dead") else None
    )
    health_info = st.get("Health") or {}
    health = str(health_info.get("Status", "")).lower() or None
    if health == "unhealthy" and code is None:
        # Kept container: the healthcheck prints the exit code of the command.
        for entry in reversed(health_info.get("Log") or []):
            out = str(entry.get("Output", "")).strip()
            if re.fullmatch(r"-?\d+", out):
                code = int(out)
                break
    return ContainerState(
        name=str(data.get("Name", "")).lstrip("/"),
        state=state,
        exit_code=code,
        health=health,
        labels={str(k): str(v) for k, v in (cfg.get("Labels") or {}).items()},
        image=str(cfg.get("Image", "")),
        id=str(data.get("Id", "")),
        started_at=parse_docker_time(st.get("StartedAt")),
        finished_at=parse_docker_time(st.get("FinishedAt")),
        created_at=parse_docker_time(data.get("Created")),
    )


def status_from_container(c: ContainerState, *, stop_requested: bool) -> RunStatus:
    """TrainHub status matching the Docker state of a container."""
    if c.command_done:
        if stop_requested:
            return RunStatus.STOPPED
        return RunStatus.SUCCEEDED if c.exit_code == 0 else RunStatus.FAILED
    if c.state == "running" or c.state == "restarting":
        return RunStatus.STOPPING if stop_requested else RunStatus.RUNNING
    if c.state == "paused":
        return RunStatus.STOPPING if stop_requested else RunStatus.PAUSED
    if c.state == "created":
        return RunStatus.LAUNCHING
    if c.state in ("exited", "dead"):
        if stop_requested:
            return RunStatus.STOPPED
        return RunStatus.SUCCEEDED if c.exit_code == 0 else RunStatus.FAILED
    return RunStatus.FAILED


# -- durations -----------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC)


def elapsed_seconds(run: Run, now: datetime | None = None) -> float:
    """Elapsed time: ``now - started_at`` while active, else ``finished_at - started_at``.

    Pause time is included.
    """
    if run.started_at is None:
        return 0.0
    end = run.finished_at if run.finished_at is not None else (now or utcnow())
    return max(0.0, (end - run.started_at).total_seconds())


def paused_seconds(run: Run, now: datetime | None = None) -> float:
    """Cumulative paused duration, including the current pause."""
    total = run.paused_total_s
    if run.paused_at is not None:
        total += max(0.0, ((now or utcnow()) - run.paused_at).total_seconds())
    return total


def format_duration(seconds: float) -> str:
    """Format a duration: ``12s``, ``4m 05s``, ``3h 12m``, ``2d 04h``."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h:02d}h"
