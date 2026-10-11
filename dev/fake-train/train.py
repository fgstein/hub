"""Faux entraînement Hydra pour tester TrainHub en mode local / sim.

- lit sa config Hydra (``--config-name``) et accepte les overrides ;
- simule des époques avec une barre tqdm ;
- gère SIGTERM (écrit un message et sort avec le code 143) ;
- échoue sur commande : ``fail_at_epoch=N`` (traceback, code 1).
"""

from __future__ import annotations

import logging
import math
import os
import random
import signal
import sys
import time
from pathlib import Path

import hydra
from omegaconf import DictConfig, OmegaConf
from tqdm import tqdm

log = logging.getLogger(__name__)
_stop = False


def _on_sigterm(signum: int, frame: object) -> None:
    global _stop
    _stop = True


@hydra.main(version_base=None, config_path="configs", config_name="baseline")
def main(cfg: DictConfig) -> None:
    signal.signal(signal.SIGTERM, _on_sigterm)
    venv = Path(sys.prefix).name
    log.info("venv=%s python=%s pid=%d", venv, sys.version.split()[0], os.getpid())
    log.info("Configuration résolue :\n%s", OmegaConf.to_yaml(cfg))
    rng = random.Random(cfg.seed)
    loss = 2.3
    for epoch in range(1, cfg.trainer.max_epochs + 1):
        bar = tqdm(
            range(cfg.trainer.steps),
            desc=f"Epoch {epoch}/{cfg.trainer.max_epochs}",
            dynamic_ncols=False,
            ncols=100,
        )
        for step in bar:
            if _stop:
                bar.close()
                log.warning("SIGTERM reçu : sauvegarde du checkpoint et arrêt")
                sys.exit(143)
            time.sleep(cfg.trainer.step_s)
            loss *= rng.uniform(0.995, 1.0)
            bar.set_postfix(loss=f"{loss:.3f}")
            if cfg.fail_at_epoch == epoch and step == cfg.trainer.steps // 2:
                bar.close()
                raise RuntimeError(f"échec simulé à l'époque {epoch} (fail_at_epoch)")
        acc = 0.5 + 0.45 * (1 - math.exp(-epoch / 3))
        log.info("epoch=%d train/loss=%.4f val/acc=%.4f", epoch, loss, acc)
    log.info("Entraînement terminé")


if __name__ == "__main__":
    main()
