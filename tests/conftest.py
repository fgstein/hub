from __future__ import annotations

from pathlib import Path

import pytest

from trainhub.core.inventory import parse_inventory
from trainhub.core.models import Inventory

ROOT = Path(__file__).resolve().parent.parent

BASIC_INVENTORY = """
machines:
  - id: gpu-01
    label: GPU 01
    host: gpu-01.lab.local
    user: alice
  - id: gpu-02
    host: gpu-02
  - id: local
    host: localhost
images:
  - id: torch-2.4
    image: registry.lab/ml/torch:2.4-cuda12
  - id: torch-nightly
    image: registry.lab/ml/torch:nightly
venvs:
  - id: main
    path: /opt/venvs/main
  - id: exp
    path: /opt/venvs/exp
    images: [torch-nightly]
projects:
  - id: segmentation
    label: Segmentation
    command: python train.py --config-name {config}
    workdir: /workspace/seg
    machines: [gpu-01, gpu-02]
    images: [torch-2.4, torch-nightly]
    venvs: [main, exp]
    docker:
      gpus: all
      shm_size: 16g
      volumes: [/data:/data:ro]
      env: {WANDB_MODE: online}
    configs:
      static: [baseline, unet_large]
  - id: detection
    command: python -m det.train --config-name {config} run_id={run_id}
    configs:
      discover: {path: /home/alice/det/configs}
"""


@pytest.fixture
def inventory() -> Inventory:
    return parse_inventory(BASIC_INVENTORY, "test.yaml")
