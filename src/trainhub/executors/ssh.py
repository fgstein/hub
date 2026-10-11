"""``ssh`` executor (production): the system ``ssh`` binary.

Honors ``~/.ssh/config``, the SSH agent and jump hosts. ``ControlMaster``/``ControlPersist``
reuse one connection per machine, which makes actions almost instantaneous. A ``localhost``
machine (without user or port) is run without SSH.
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
    """Wrap every command in ``ssh -o ControlMaster=auto … host -- CMD``."""

    kind = "ssh"
    transport_error_code = 255

    def __init__(self, extra_options: Sequence[str] = SSH_BATCH_OPTIONS) -> None:
        self.extra_options = tuple(extra_options)
        # The ControlPath lives in ~/.ssh: make sure the directory exists.
        with contextlib.suppress(OSError):
            (Path.home() / ".ssh").mkdir(mode=0o700, exist_ok=True)

    def full_argv(self, machine: Machine, remote: Sequence[str]) -> list[str]:
        return transport(machine, remote, extra=self.extra_options)

    def shell_argv(self, machine: Machine, remote: Sequence[str]) -> list[str]:
        # -tt: a remote terminal is required for ``docker exec -it``.
        return transport(machine, remote, extra=(*self.extra_options, "-tt"))

    def _env(self) -> dict[str, str] | None:
        return dict(os.environ)
