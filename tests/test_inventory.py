from __future__ import annotations

import pytest

from trainhub.core.inventory import InventoryError, load_inventory, parse_inventory
from trainhub.core.models import Inventory, UnknownResourceError

from .conftest import BASIC_INVENTORY, ROOT


def test_example_inventory_is_valid() -> None:
    inv = load_inventory(ROOT / "inventory.example.yaml")
    assert [p.id for p in inv.projects] == ["segmentation"]
    seg = inv.project("segmentation")
    assert seg.docker.extra_args == ["--ipc=host"]
    assert seg.configs.discover is not None and seg.configs.discover.pattern == "*.yaml"


def test_dev_inventories_are_valid() -> None:
    for name in ("inventory.dev.yaml", "inventory.sim.yaml", "inventory.local.yaml"):
        path = ROOT / "dev" / name
        if path.exists():
            load_inventory(path)


def test_allowed_resources(inventory: Inventory) -> None:
    seg = inventory.project("segmentation")
    assert [m.id for m in inventory.allowed_machines(seg)] == ["gpu-01", "gpu-02"]
    assert [v.id for v in inventory.allowed_venvs(seg, "torch-2.4")] == ["main", "system"]
    assert [v.id for v in inventory.allowed_venvs(seg, "torch-nightly")] == [
        "main",
        "exp",
        "system",
    ]
    assert inventory.venv("system").is_system
    det = inventory.project("detection")
    assert len(inventory.allowed_machines(det)) == 3


def test_unknown_resource_message(inventory: Inventory) -> None:
    with pytest.raises(UnknownResourceError) as ei:
        inventory.machine("nope")
    assert "gpu-01, gpu-02, local" in str(ei.value)


def test_error_has_file_line_and_field() -> None:
    text = BASIC_INVENTORY.replace(
        "command: python train.py --config-name {config}", "command: python train.py"
    )
    with pytest.raises(InventoryError) as ei:
        parse_inventory(text, "inv.yaml")
    msg = str(ei.value)
    line = next(
        i for i, ln in enumerate(text.splitlines(), 1) if ln.strip() == "command: python train.py"
    )
    assert f"inv.yaml:{line}" in msg
    assert "projects[0].command" in msg
    assert "{config}" in msg


def test_free_placeholders_are_params() -> None:
    from trainhub.core.models import command_params

    text = BASIC_INVENTORY.replace("{config}\n", "{config} {options} --x {opt2} {options}\n", 1)
    inv = parse_inventory(text)
    assert command_params(inv.project("segmentation").command) == ["options", "opt2"]
    assert command_params("python t.py {config} {run_id} ${HOME} {machine}") == []


def test_command_without_config_rejected() -> None:
    text = BASIC_INVENTORY.replace("{config}\n", "{gpu}\n", 1)
    with pytest.raises(InventoryError, match=r"\{config\}"):
        parse_inventory(text)


def test_shell_vars_are_not_placeholders() -> None:
    text = BASIC_INVENTORY.replace(
        "{config}\n", "{config} 'paths.out=${oc.env:HOME}' ${EXTRA}\n", 1
    )
    parse_inventory(text)


def test_unknown_reference() -> None:
    text = BASIC_INVENTORY.replace("machines: [gpu-01, gpu-02]", "machines: [gpu-01, gpu-09]")
    with pytest.raises(InventoryError, match="gpu-09"):
        parse_inventory(text)


def test_venv_image_reference() -> None:
    text = BASIC_INVENTORY.replace("images: [torch-nightly]", "images: [torch-9]")
    with pytest.raises(InventoryError, match="torch-9"):
        parse_inventory(text)


def test_duplicate_ids() -> None:
    text = BASIC_INVENTORY.replace("id: gpu-02", "id: gpu-01")
    with pytest.raises(InventoryError, match="dupliqué"):
        parse_inventory(text)


def test_extra_field_rejected_with_line() -> None:
    text = BASIC_INVENTORY.replace("    host: gpu-02\n", "    host: gpu-02\n    hots: x\n")
    with pytest.raises(InventoryError) as ei:
        parse_inventory(text, "f.yaml")
    assert "machines[1].hots" in str(ei.value)


def test_invalid_yaml() -> None:
    with pytest.raises(InventoryError, match=r"f.yaml:\d+ : YAML invalide"):
        parse_inventory("machines: [\n  - a: b\n", "f.yaml")


def test_empty_inventory() -> None:
    assert parse_inventory("").projects == []


def test_missing_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InventoryError, match="lecture impossible"):
        load_inventory(tmp_path / "absent.yaml")


def test_configs_root_shorthand() -> None:
    text = BASIC_INVENTORY.replace(
        "      static: [baseline, unet_large]",
        "      root: /cfg\n      exclude: ['hydra/*']",
    )
    inv = parse_inventory(text)
    d = inv.project("segmentation").configs.discover
    assert d is not None and d.path == "/cfg" and d.exclude == ["hydra/*"]
