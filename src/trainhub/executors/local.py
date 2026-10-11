"""``local`` executor: real docker commands, without SSH, on the TrainHub host.

Every machine of the inventory runs locally; runs are told apart by the
``trainhub.machine`` label.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..core.models import Machine
from .subprocess_exec import SubprocessExecutor


class LocalExecutor(SubprocessExecutor):
    """Run the remote argv as is, locally."""

    kind = "local"

    def full_argv(self, machine: Machine, remote: Sequence[str]) -> list[str]:
        return list(remote)
