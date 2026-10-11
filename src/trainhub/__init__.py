"""TrainHub : lance et suit des entraînements Hydra sur des machines distantes."""

from __future__ import annotations

__version__ = "0.1.0"

from .client import TrainHub
from .core.models import Run, RunSource, RunStatus

__all__ = ["Run", "RunSource", "RunStatus", "TrainHub", "__version__"]
