"""Validation of a selection (project, machine, image, venv, config) against the inventory."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .commands import RunSpec
from .models import Inventory, command_params


class SelectionError(ValueError):
    """Selection incompatible with the inventory restrictions."""


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
    params: Mapping[str, str] | None = None,
) -> RunSpec:
    """Build a :class:`RunSpec`, checking the resources allowed for the project.

    Raises :class:`~trainhub.core.models.UnknownResourceError` or :class:`SelectionError`
    with an actionable message (possible values).
    """
    proj = inv.project(project)
    m = inv.machine(machine)
    i = inv.image(image)
    v = inv.venv(venv)
    template = command if command is not None else proj.command
    wanted = command_params(template)
    unknown = sorted(set(params or {}) - set(wanted))
    if unknown:
        raise SelectionError(
            f"parameter(s) not in the command: {', '.join(unknown)}; "
            f"command parameters: {', '.join(wanted) or 'none'}"
        )
    machines = [x.id for x in inv.allowed_machines(proj)]
    if m.id not in machines:
        raise SelectionError(
            f"machine '{m.id}' not allowed for project '{proj.id}'; allowed: {', '.join(machines)}"
        )
    images = [x.id for x in inv.allowed_images(proj)]
    if i.id not in images:
        raise SelectionError(
            f"image '{i.id}' not allowed for project '{proj.id}'; allowed: {', '.join(images)}"
        )
    venvs = [x.id for x in inv.allowed_venvs(proj, i.id)]
    if v.id not in venvs:
        raise SelectionError(
            f"venv '{v.id}' not available for project '{proj.id}' with image '{i.id}'; "
            f"available: {', '.join(venvs) or 'none'}"
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
        params={k: (params or {}).get(k, "") for k in wanted},
    )
