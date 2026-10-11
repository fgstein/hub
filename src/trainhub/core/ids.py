"""Short run identifiers (e.g. ``r7f3k2``)."""

from __future__ import annotations

import secrets

_ALPHABET = "0123456789abcdefghijkmnpqrstuvwxyz"


def new_run_id(length: int = 5) -> str:
    """Short identifier starting with ``r`` (6 characters by default)."""
    return "r" + "".join(secrets.choice(_ALPHABET) for _ in range(length))
