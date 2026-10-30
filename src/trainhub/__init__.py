"""TrainHub: launch and monitor training commands on remote machines."""

from __future__ import annotations

__version__ = "0.4.0"

from .client import TrainHub
from .core.models import Run, RunSource, RunStatus
from .service.schemas import RandomRange, SeriesRequest

__all__ = [
    "RandomRange",
    "Run",
    "RunSource",
    "RunStatus",
    "SeriesRequest",
    "TrainHub",
    "__version__",
]
