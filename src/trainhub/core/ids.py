"""Identifiants courts de runs (ex. ``r7f3k2``)."""

from __future__ import annotations

import secrets

_ALPHABET = "0123456789abcdefghijkmnpqrstuvwxyz"


def new_run_id(length: int = 5) -> str:
    """Identifiant court commençant par ``r`` (6 caractères par défaut)."""
    return "r" + "".join(secrets.choice(_ALPHABET) for _ in range(length))
