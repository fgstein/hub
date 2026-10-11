"""Exécuteur ``ssh`` (production) : binaire ``ssh`` du système.

Respecte ``~/.ssh/config``, l'agent SSH et les jump hosts. ``ControlMaster``/``ControlPersist``
réutilisent une connexion par machine, ce qui rend les actions quasi instantanées. Une machine
``localhost`` (sans user ni port) est exécutée sans SSH.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Sequence
from pathlib import Path

from ..core.commands import SSH_BATCH_OPTIONS, transport
from ..core.models import Machine
from .subprocess_exec import SubprocessExecutor


class SshExecutor(SubprocessExecutor):
    """Enveloppe chaque commande dans ``ssh -o ControlMaster=auto … host -- CMD``."""

    kind = "ssh"
    transport_error_code = 255

    def __init__(self, extra_options: Sequence[str] = SSH_BATCH_OPTIONS) -> None:
        self.extra_options = tuple(extra_options)
        # Le ControlPath vit dans ~/.ssh : on s'assure que le répertoire existe.
        with contextlib.suppress(OSError):
            (Path.home() / ".ssh").mkdir(mode=0o700, exist_ok=True)

    def full_argv(self, machine: Machine, remote: Sequence[str]) -> list[str]:
        return transport(machine, remote, extra=self.extra_options)

    def shell_argv(self, machine: Machine, remote: Sequence[str]) -> list[str]:
        # -tt : terminal distant obligatoire pour ``docker exec -it``.
        return transport(machine, remote, extra=(*self.extra_options, "-tt"))

    def _env(self) -> dict[str, str] | None:
        return dict(os.environ)
