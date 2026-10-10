"""Validation d'une sélection (projet, machine, image, venv, config) contre l'inventaire."""

from __future__ import annotations

from collections.abc import Sequence

from .commands import RunSpec
from .models import Inventory


class SelectionError(ValueError):
    """Sélection incompatible avec les restrictions de l'inventaire."""


def make_spec(
    inv: Inventory,
    *,
    run_id: str,
    project: str,
    machine: str,
    image: str,
    venv: str,
    config: str,
    command: str | None = None,
    overrides: Sequence[str] = (),
    cuda_visible_devices: str | None = None,
) -> RunSpec:
    """Construit un :class:`RunSpec` en vérifiant les ressources autorisées pour le projet.

    Lève :class:`~trainhub.core.models.UnknownResourceError` ou :class:`SelectionError`
    avec un message actionnable (valeurs possibles).
    """
    proj = inv.project(project)
    m = inv.machine(machine)
    i = inv.image(image)
    v = inv.venv(venv)
    machines = [x.id for x in inv.allowed_machines(proj)]
    if m.id not in machines:
        raise SelectionError(
            f"machine '{m.id}' non autorisée pour le projet '{proj.id}' ; "
            f"autorisées : {', '.join(machines)}"
        )
    images = [x.id for x in inv.allowed_images(proj)]
    if i.id not in images:
        raise SelectionError(
            f"image '{i.id}' non autorisée pour le projet '{proj.id}' ; "
            f"autorisées : {', '.join(images)}"
        )
    venvs = [x.id for x in inv.allowed_venvs(proj, i.id)]
    if v.id not in venvs:
        raise SelectionError(
            f"venv '{v.id}' non disponible pour le projet '{proj.id}' avec l'image '{i.id}' ; "
            f"disponibles : {', '.join(venvs) or 'aucun'}"
        )
    cvd = cuda_visible_devices.strip() if cuda_visible_devices else None
    return RunSpec(
        run_id=run_id,
        project=proj,
        machine=m,
        image=i,
        venv=v,
        config=config.strip(),
        command=command,
        overrides=tuple(o for o in overrides if o.strip()),
        cuda_visible_devices=cvd or None,
    )
