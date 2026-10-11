"""GPU state of a machine through ``nvidia-smi`` (pure functions, no I/O).

Two queries run on the machine itself (not in a container):

- :func:`query_gpus`: one CSV line per GPU (index, uuid, name, memory, utilization,
  temperature);
- :func:`query_apps`: one CSV line per compute process (GPU uuid, pid, memory), used to count
  the processes of each GPU.

``[N/A]`` / ``[Not Supported]`` values become ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass

GPU_FIELDS = (
    "index",
    "uuid",
    "name",
    "memory.used",
    "memory.total",
    "utilization.gpu",
    "temperature.gpu",
)
APP_FIELDS = ("gpu_uuid", "pid", "used_memory")


@dataclass(frozen=True)
class GpuInfo:
    index: int
    uuid: str
    name: str
    memory_used_mib: int | None
    memory_total_mib: int | None
    utilization_pct: int | None
    temperature_c: int | None
    processes: int = 0


def query_gpus() -> list[str]:
    return [
        "nvidia-smi",
        f"--query-gpu={','.join(GPU_FIELDS)}",
        "--format=csv,noheader,nounits",
    ]


def query_apps() -> list[str]:
    return [
        "nvidia-smi",
        f"--query-compute-apps={','.join(APP_FIELDS)}",
        "--format=csv,noheader,nounits",
    ]


def _int(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None


def _rows(output: str, width: int) -> list[list[str]]:
    rows = []
    for line in output.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) >= width:
            # a GPU name could contain a comma: glue the extra cells back together
            extra = len(cells) - width
            if extra:
                cells[2 : 3 + extra] = [", ".join(cells[2 : 3 + extra])]
            rows.append(cells)
    return rows


def parse_gpus(gpus_csv: str, apps_csv: str = "") -> list[GpuInfo]:
    """Parse the output of :func:`query_gpus` (and :func:`query_apps` for process counts)."""
    per_uuid: dict[str, int] = {}
    for cells in _rows(apps_csv, len(APP_FIELDS)):
        per_uuid[cells[0]] = per_uuid.get(cells[0], 0) + 1
    out = []
    for cells in _rows(gpus_csv, len(GPU_FIELDS)):
        index = _int(cells[0])
        if index is None:
            continue
        out.append(
            GpuInfo(
                index=index,
                uuid=cells[1],
                name=cells[2],
                memory_used_mib=_int(cells[3]),
                memory_total_mib=_int(cells[4]),
                utilization_pct=_int(cells[5]),
                temperature_c=_int(cells[6]),
                processes=per_uuid.get(cells[1], 0),
            )
        )
    return sorted(out, key=lambda g: g.index)


def visible_indices(cuda_visible_devices: str | None) -> list[int] | None:
    """GPU indices of a ``CUDA_VISIBLE_DEVICES`` value (``None``: no restriction)."""
    if cuda_visible_devices is None:
        return None
    value = cuda_visible_devices.replace(" ", "")
    if value == "all":
        return None
    if value in ("", "none", "void"):
        return []
    return [int(p) for p in value.split(",") if p.isdigit()]
