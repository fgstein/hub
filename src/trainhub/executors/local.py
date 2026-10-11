"""Exécuteur ``local`` : vraies commandes docker, sans SSH, sur la machine de TrainHub.

Toutes les machines de l'inventaire sont exécutées localement ; les runs sont distingués par
le label ``trainhub.machine``.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..core.models import Machine
from .subprocess_exec import SubprocessExecutor


class LocalExecutor(SubprocessExecutor):
    """Exécute l'argv distant tel quel, localement."""

    kind = "local"

    def full_argv(self, machine: Machine, remote: Sequence[str]) -> list[str]:
        return list(remote)
