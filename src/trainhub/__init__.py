"""TrainHub: launch and monitor training commands on remote machines."""

from __future__ import annotations

__version__ = "0.2.0"

from .client import TrainHub
from .core.models import Run, RunSource, RunStatus

__all__ = ["Run", "RunSource", "RunStatus", "TrainHub", "__version__"]
