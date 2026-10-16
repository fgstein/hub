"""CPU and memory load of a machine, read from ``/proc`` (pure functions, no I/O).

A single command runs on the machine (host, not in a container): ``/proc/loadavg``, two
readings of the ``cpu`` lines of ``/proc/stat`` half a second apart (usage of each core over
that interval), and ``MemTotal`` / ``MemAvailable`` of ``/proc/meminfo``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEPARATOR = "---"
#: Interval between the two readings of ``/proc/stat``.
SAMPLE_S = 0.5
CPU_SCRIPT = (
    f"cat /proc/loadavg; echo {SEPARATOR}; grep '^cpu' /proc/stat; sleep {SAMPLE_S}; "
    f"echo {SEPARATOR}; grep '^cpu' /proc/stat; echo {SEPARATOR}; "
    "grep -E '^(MemTotal|MemAvailable):' /proc/meminfo"
)


@dataclass(frozen=True)
class CpuInfo:
    cores: int
    usage_pct: float
    per_core_pct: list[float] = field(default_factory=list)
    load_avg: tuple[float, float, float] | None = None
    memory_used_mib: int | None = None
    memory_total_mib: int | None = None


def query_cpu() -> list[str]:
    return ["sh", "-c", CPU_SCRIPT]


def _stat(lines: list[str]) -> dict[str, tuple[int, int]]:
    """``{"cpu": (busy, total), "cpu0": …}`` of the ``cpu`` lines of ``/proc/stat``."""
    out = {}
    for line in lines:
        parts = line.split()
        if not parts or not parts[0].startswith("cpu"):
            continue
        try:
            # user nice system idle iowait irq softirq steal (guest is included in user)
            values = [int(v) for v in parts[1:9]]
        except ValueError:
            continue
        if len(values) < 4:
            continue
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        out[parts[0]] = (total - idle, total)
    return out


def _usage(before: tuple[int, int], after: tuple[int, int]) -> float:
    busy, total = after[0] - before[0], after[1] - before[1]
    if total <= 0:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 * busy / total)), 1)


def parse_cpu(output: str) -> CpuInfo:
    """Parse the output of :func:`query_cpu`; raise ``ValueError`` if it is unusable."""
    sections = [s.strip().splitlines() for s in output.split(f"\n{SEPARATOR}\n")]
    if len(sections) < 3:
        raise ValueError("unexpected output (is /proc available?)")
    load: tuple[float, float, float] | None = None
    try:
        a, b, c = (float(x) for x in sections[0][0].split()[:3])
        load = (a, b, c)
    except (IndexError, ValueError):
        pass
    before, after = _stat(sections[1]), _stat(sections[2])
    if "cpu" not in before or "cpu" not in after:
        raise ValueError("no cpu line in /proc/stat")
    cores = sorted((k for k in after if k != "cpu" and k in before), key=lambda k: int(k[3:] or 0))
    mem: dict[str, int] = {}
    for line in sections[3] if len(sections) > 3 else []:
        key, _, rest = line.partition(":")
        try:
            mem[key.strip()] = int(rest.split()[0]) // 1024  # kB → MiB
        except (IndexError, ValueError):
            continue
    total = mem.get("MemTotal")
    avail = mem.get("MemAvailable")
    return CpuInfo(
        cores=len(cores) or 1,
        usage_pct=_usage(before["cpu"], after["cpu"]),
        per_core_pct=[_usage(before[k], after[k]) for k in cores],
        load_avg=load,
        memory_used_mib=total - avail if total is not None and avail is not None else None,
        memory_total_mib=total,
    )
