"""Exécuteur SSH avec un faux binaire ``ssh`` (pas de réseau)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from trainhub.core.models import Machine
from trainhub.executors.base import MachineUnreachableError
from trainhub.executors.ssh import SshExecutor

FAKE_SSH = r"""#!/usr/bin/env bash
# Faux ssh : journalise l'argv puis exécute la commande distante avec sh -c,
# comme le ferait le shell de connexion de la machine.
printf '%s\n' "$@" > "$FAKE_SSH_LOG"
host=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-p) shift 2 ;;
    --) shift; break ;;
    *) host="$1"; shift ;;
  esac
done
case "$host" in
  *down*) echo "ssh: connect to host $host port 22: Connection refused" >&2; exit 255 ;;
  *slow*) sleep 5 ;;
esac
exec sh -c "$1"
"""


@pytest.fixture
def fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return log


async def test_exec_through_ssh(fake_ssh: Path) -> None:
    ex = SshExecutor()
    m = Machine(id="m", host="gpu.lab", user="alice", port=2222)
    res = await ex.exec(m, ["printf", "%s|", "a b", "it's", "$HOME"])
    assert res.ok and res.out == "a b|it's|$HOME|"
    argv = fake_ssh.read_text().splitlines()
    assert "ControlMaster=auto" in argv and "BatchMode=yes" in argv
    assert argv[argv.index("-p") + 1] == "2222"
    assert "alice@gpu.lab" in argv


async def test_unreachable_is_reported(fake_ssh: Path) -> None:
    ex = SshExecutor()
    with pytest.raises(MachineUnreachableError, match="Connection refused"):
        await ex.exec(Machine(id="m", host="down.lab"), ["true"])


async def test_timeout_is_unreachable(fake_ssh: Path) -> None:
    ex = SshExecutor()
    with pytest.raises(MachineUnreachableError, match="délai dépassé"):
        await ex.exec(Machine(id="m", host="slow.lab"), ["true"], timeout=0.5)


async def test_remote_failure_is_not_unreachable(fake_ssh: Path) -> None:
    ex = SshExecutor()
    res = await ex.exec(Machine(id="m", host="gpu.lab"), ["sh", "-c", "echo boom >&2; exit 3"])
    assert res.returncode == 3 and res.error_message() == "boom"


async def test_stream(fake_ssh: Path) -> None:
    ex = SshExecutor()
    stream = await ex.stream(Machine(id="m", host="gpu.lab"), ["sh", "-c", "echo a; echo b >&2"])
    data = b"".join([c async for c in stream])
    assert data.splitlines() == [b"a", b"b"]
    await stream.close()


async def test_localhost_skips_ssh(fake_ssh: Path) -> None:
    ex = SshExecutor()
    res = await ex.exec(Machine(id="local", host="localhost"), ["echo", "hi"])
    assert res.out == "hi\n" and not fake_ssh.exists()


async def test_interactive_shell_through_pty(fake_ssh: Path) -> None:
    import asyncio

    ex = SshExecutor()
    script = "stty size; read x; echo got:$x; stty size"
    shell = await ex.open_shell(Machine(id="m", host="gpu.lab"), ["sh", "-c", script])
    out = b""

    async def read_until(needle: bytes) -> None:
        nonlocal out
        while needle not in out:
            chunk = await asyncio.wait_for(shell.read(), 5)
            assert chunk, out
            out += chunk

    try:
        await read_until(b"32 120")  # taille initiale du PTY (lignes colonnes)
        shell.resize(100, 40)
        shell.write(b"hello\r")
        await read_until(b"got:hello")
        await read_until(b"40 100")
        assert "-tt" in fake_ssh.read_text().splitlines()
        assert await asyncio.wait_for(shell.read(), 5) in (b"", b"\r\n")
    finally:
        await shell.close()
