"""Series: the same run repeated several times in a row, in a single container.

A series is one TrainHub run (one container) whose script runs the command ``count`` times,
one after the other. Every repetition uses the same selection; only the parameters drawn at
random (e.g. ``{seed}``) change. The values are drawn once, at launch, and stored with the run
(:class:`~trainhub.core.models.Series`): a relaunch reproduces them exactly.

The script prints a marker line when each repetition starts and finishes (see
:func:`start_marker`, :func:`end_marker`); TrainHub follows the progress by reading them in
the logs (:class:`SeriesTracker`), without any extra command on the machine.
"""

from __future__ import annotations

import random as _random
import re
from collections.abc import Mapping

#: Maximum number of repetitions of a series.
MAX_SERIES = 100
#: Default range of a random parameter (non-negative 32-bit integer, a usual seed range).
DEFAULT_RANGE = (0, 2**31 - 1)
#: Variable exported to each repetition: its index, from 1.
INDEX_ENV = "TRAINHUB_SERIES_INDEX"
#: MLflow tag (in ``MLFLOW_RUN_CONTEXT``) holding the index of the repetition.
INDEX_TAG = "trainhub.series_index"

_MARKER_RE = re.compile(
    rb"\[trainhub\] series run (\d+)/(\d+) (?:started|finished \(code (-?\d+)\))"
)
#: Longest partial line kept between two chunks of logs.
_CARRY_MAX = 512


def draw_values(
    ranges: Mapping[str, tuple[int, int]], count: int, rng: _random.Random | None = None
) -> list[dict[str, str]]:
    """Values of the random parameters for each repetition (``count`` dicts).

    Each parameter gets distinct values when its range is large enough, otherwise values may
    repeat.
    """
    rng = rng or _random.SystemRandom()
    per_param: dict[str, list[int]] = {}
    for name, (lo, hi) in ranges.items():
        span = range(lo, hi + 1)
        if len(span) >= count:
            per_param[name] = rng.sample(span, count)
        else:
            per_param[name] = [rng.randint(lo, hi) for _ in range(count)]
    return [{name: str(vals[i]) for name, vals in per_param.items()} for i in range(count)]


def describe_values(values: Mapping[str, str]) -> str:
    """``seed=4821 lr=0.1`` (shown in the logs and the interface)."""
    return " ".join(f"{k}={v}" for k, v in values.items())


def start_marker(index: int | str, count: int) -> str:
    return f"[trainhub] series run {index}/{count} started"


def end_marker(index: int | str, count: int, code: int | str) -> str:
    return f"[trainhub] series run {index}/{count} finished (code {code})"


def halt_message(index: int | str, count: int) -> str:
    return f"[trainhub] series stopped: run {index}/{count} failed, the next runs are not launched"


class SeriesTracker:
    """Progress of a series read from its logs: repetition in progress and exit codes.

    Fed with the raw chunks of logs, in order; a marker split between two chunks is
    recognized.
    """

    def __init__(self) -> None:
        self.current = 0
        self.exit_codes: list[int] = []
        self._carry = b""

    @property
    def progress(self) -> int:
        """Number of markers seen so far (grows by one at each start and each end)."""
        return progress(self.current, self.exit_codes)

    def feed(self, data: bytes) -> bool:
        """Read a chunk of logs; ``True`` if the progress changed."""
        text = self._carry + data
        cut = max(text.rfind(b"\n"), text.rfind(b"\r"))
        if cut < 0:
            self._carry = text[-_CARRY_MAX:]
            return False
        complete, self._carry = text[: cut + 1], text[cut + 1 :][-_CARRY_MAX:]
        if b"[trainhub] series run" not in complete:
            return False
        before = self.progress
        for m in _MARKER_RE.finditer(complete):
            index = int(m.group(1))
            if m.group(3) is None:
                self.current = index
                del self.exit_codes[index - 1 :]
            elif index == len(self.exit_codes) + 1:
                self.current = index
                self.exit_codes.append(int(m.group(3)))
        return self.progress != before


def progress(current: int, exit_codes: list[int]) -> int:
    """Number of markers matching a state: ``current`` starts and ``len(exit_codes)`` ends."""
    return current + len(exit_codes)
