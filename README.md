# TrainHub

TrainHub lance et suit des entraînements [Hydra](https://hydra.cc) sur des machines distantes,
depuis une interface web, un serveur MCP (pour les agents) ou en Python pur.

Chaque run est construit à partir de 4 choix (**machine**, **image Docker**, **venv**, **config**)
et exécute la chaîne :

```
ssh machine → docker run -d (conteneur conservé) → source venv → cd workdir → python train.py --config-name XXX
```

- Projets en onglets, chacun avec sa commande générique éditable (`{config}`, `{run_id}`, `{machine}`, `{project}`).
- Suivi en direct : statut, temps écoulé, logs en streaming dans un terminal xterm.js (couleurs ANSI, barres tqdm).
- Contrôle : pause, reprise, arrêt, redémarrage, relance, suppression.
- Les mêmes actions pour un agent via MCP (Streamable HTTP sur `/mcp`, ou stdio avec `trainhub mcp`).
- Développement et tests sans GPU grâce aux modes `fake` (simulation en mémoire) et `local` (Docker local).

La spécification d'origine est l'issue #1 du dépôt ; les choix et écarts sont listés dans
[`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Démarrage rapide (développement, mode fake)

Prérequis : [uv](https://docs.astral.sh/uv/), Node 22+.

```bash
make install   # uv sync + npm install
make dev       # backend en mode fake (rechargement) + Vite HMR → http://localhost:5173
```

`make dev` utilise `dev/inventory.dev.yaml` : 4 machines simulées (dont une instable et une hors
ligne), 3 projets, des logs réalistes (bannière Hydra, config résolue, barres de progression,
métriques, warnings, échecs aléatoires).

Autres modes :

| Commande         | Exécuteur | Ce qui est réel                                        |
|------------------|-----------|--------------------------------------------------------|
| `make dev`       | `fake`    | Rien : machines et conteneurs simulés en mémoire        |
| `make dev-local` | `local`   | Docker local, sans SSH (image `trainhub-fake-train`)    |
| `make dev-sim`   | `ssh`     | SSH + Docker, vers 2 machines simulées (docker compose) |

`make dev-sim` génère une clé de test (`dev/ssh/`) ; ajoutez la ligne
`Include <chemin>/dev/ssh/config` à `~/.ssh/config` pour que les alias `sim-gpu-01/02` résolvent.

Qualité :

```bash
make lint      # ruff, mypy --strict, biome, tsc
make test      # pytest + vitest
make e2e       # Playwright (mode fake)
make build     # front + paquet Python
make image     # image Docker trainhub:latest
```

Les tests d'intégration Docker (`tests/test_local_docker.py`) et SSH (`tests/test_sim_ssh.py`)
sont ignorés automatiquement si Docker, l'image de test ou les machines simulées sont absents.

---

## Inventaire (`inventory.yaml`)

Les ressources sont déclarées dans un fichier YAML en lecture seule, validé au démarrage avec des
erreurs lisibles (fichier, ligne, champ) et **rechargé à chaud** quand il change (un fichier
devenu invalide est ignoré : l'ancien inventaire reste actif et l'erreur s'affiche en bas de page).

Exemple complet commenté : [`inventory.example.yaml`](inventory.example.yaml).

```yaml
machines:
  - id: gpu-01
    host: gpu-01.lab.local      # ou un alias de ~/.ssh/config ; « localhost » = sans SSH
    user: alice                 # optionnel
images:
  - id: torch-2.4
    image: registry.lab/ml/torch:2.4-cuda12
venvs:
  - id: main
    path: /opt/venvs/main        # chemin DANS le conteneur
projects:
  - id: segmentation
    command: python train.py --config-name {config}
    workdir: /workspace/seg
    docker: {gpus: all, shm_size: 16g, volumes: [/data:/data:ro], tty: true, stop_timeout: 30}
    configs:
      static: [baseline, unet_large]
      discover: {path: /home/alice/seg/configs, pattern: "*.yaml"}   # ls via SSH, cache 60 s
```

L'état modifiable depuis l'interface (commande surchargée d'un projet, onglets ouverts, dernières
sélections, runs) vit dans une base SQLite (`$TRAINHUB_DATA_DIR/trainhub.sqlite`).

---

## Déploiement (Docker)

TrainHub tourne dans un conteneur et n'a pas besoin de Docker local : tout `docker` s'exécute sur
les machines distantes via le binaire `ssh` du système (respect de `~/.ssh/config`, de l'agent
SSH et des jump hosts ; `ControlMaster`/`ControlPersist` réutilisent les connexions).

```bash
make image   # ou : docker build -t trainhub:latest .

docker run -d --name trainhub --network host \
  -v ~/.ssh/config:/home/th/.ssh/config:ro \
  -v ~/.ssh/known_hosts:/home/th/.ssh/known_hosts:ro \
  -v $SSH_AUTH_SOCK:/ssh-agent -e SSH_AUTH_SOCK=/ssh-agent \
  -v ./inventory.yaml:/etc/trainhub/inventory.yaml:ro \
  -v trainhub-data:/var/lib/trainhub \
  trainhub:latest
```

Puis ouvrir <http://127.0.0.1:8420>.

**Clés privées au lieu de l'agent SSH.** Montez les clés en lecture seule et référencez-les dans
la config SSH montée (`IdentityFile /home/th/.ssh/id_ed25519`) :

```bash
  -v ~/.ssh/id_ed25519:/home/th/.ssh/id_ed25519:ro \
```

Permissions attendues par `ssh` : clé privée `600` (ou `400`), `config` `600` ou `644` non
modifiable par d'autres, appartenant à l'utilisateur du conteneur. L'utilisateur `th` a l'UID
1000 : si vos fichiers appartiennent à un autre UID, reconstruisez avec
`docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) -t trainhub .`.
Le répertoire `/home/th/.ssh` doit rester inscriptible (sockets `ControlMaster`) : montez des
fichiers, pas le répertoire entier.

### Variables d'environnement

| Variable                | Défaut               | Rôle                                                           |
|-------------------------|----------------------|----------------------------------------------------------------|
| `TRAINHUB_INVENTORY`    | `./inventory.yaml`   | Chemin de l'inventaire (rechargé à chaud)                      |
| `TRAINHUB_DATA_DIR`     | `./.trainhub`        | Répertoire de la base SQLite                                   |
| `TRAINHUB_HOST`         | `127.0.0.1`          | Adresse d'écoute                                               |
| `TRAINHUB_PORT`         | `8420`               | Port                                                           |
| `TRAINHUB_EXECUTOR`     | `ssh`                | `ssh`, `local` ou `fake`                                       |
| `TRAINHUB_TOKEN`        | —                    | Si défini : jeton exigé partout (API, WebSocket, `/mcp`)       |
| `TRAINHUB_MCP_READONLY` | —                    | `1` : n'expose que les outils MCP de lecture                   |
| `TRAINHUB_STATIC_DIR`   | auto                 | Répertoire du front compilé (défini dans l'image)              |
| `TRAINHUB_LOG_LEVEL`    | `INFO`               | Niveau des logs JSON du serveur                                |

**Jeton.** Avec `TRAINHUB_TOKEN`, envoyer `Authorization: Bearer <jeton>` (API, MCP), ou se
connecter via la page `/login` qui pose un cookie (navigateur, WebSocket).

---

## Serveur MCP

Les outils (`list_projects`, `list_resources`, `list_configs`, `preview_command`, `launch_run`,
`list_runs`, `get_run`, `get_logs`, `pause_run`, `resume_run`, `stop_run`, `restart_run`,
`relaunch_run`, `delete_run`, `set_project_command`) appellent la même couche service que
l'interface : un run lancé par un agent apparaît immédiatement dans l'interface, avec une icône
« agent ». `get_logs` renvoie du texte nettoyé (sans ANSI, barres de progression réduites à leur
dernier état, tronqué avec mention).

```bash
# Streamable HTTP (serveur en cours d'exécution)
claude mcp add --transport http trainhub http://127.0.0.1:8420/mcp
# avec jeton
claude mcp add --transport http trainhub http://127.0.0.1:8420/mcp --header "Authorization: Bearer $TRAINHUB_TOKEN"

# stdio : relaie vers le serveur en cours (pas de seconde instance avec son propre état)
claude mcp add trainhub -- trainhub mcp --url http://127.0.0.1:8420
```

`TRAINHUB_MCP_READONLY=1` n'expose que les outils de lecture.

---

## Usage en Python pur et CLI

```python
from trainhub import TrainHub

tb = TrainHub.from_inventory("inventory.yaml")          # exécuteur : TRAINHUB_EXECUTOR (défaut ssh)
run = await tb.launch(project="segmentation", machine="gpu-01",
                      image="torch-2.4", venv="main", config="baseline")
async for chunk in tb.stream_logs(run.id):
    print(chunk.decode(), end="")
await tb.stop(run.id)
```

```bash
trainhub preview -p segmentation -m gpu-01 -i torch-2.4 -e main -c baseline trainer.max_epochs=5
trainhub launch  -p segmentation -m gpu-01 -i torch-2.4 -e main -c baseline --follow
trainhub ps [--all]
trainhub logs r7f3k2 [-f] [--clean]
trainhub stop r7f3k2           # aussi : pause, resume, rm
trainhub serve                 # serveur web (API, WebSocket, MCP, front)
trainhub mcp                   # MCP stdio relié au serveur
```

La CLI partage la base SQLite du serveur (`--data-dir` / `TRAINHUB_DATA_DIR`).

---

## Architecture

```
src/trainhub/
  core/        fonctions pures : modèles, inventaire, construction des commandes (argv, shlex),
               parsing docker ps/inspect, durées, nettoyage des logs — aucune I/O
  executors/   interface Executor ; ssh (production), local (docker local), fake (simulation)
  service/     cycle de vie des runs, SQLite, suivi (1 docker ps / machine / 3 s), hub de logs
               (1 docker logs -f partagé par run, tampon circulaire 2 Mo), réconciliation, bus
  api/         FastAPI : REST, WebSocket /ws (logs en trames binaires), jeton, front statique
  mcp/         FastMCP (HTTP sur /mcp) et proxy stdio
  client.py    classe TrainHub (Python pur) ; cli.py
web/           SolidJS + Vite + Tailwind v4 + Kobalte + xterm.js (thème inspiré d'OpenCode)
dev/           inventaires de dev, image fake-train, machines SSH simulées (docker compose)
```

Règles clés : l'API et le MCP n'appellent que la couche service ; `core` est testable sans I/O ;
aucune commande n'est construite par concaténation non échappée (échappement `shlex.quote` à
chaque niveau : shell distant, puis `bash -lc` dans le conteneur) ; fermer le navigateur ou
redémarrer TrainHub n'interrompt jamais un entraînement (conteneurs détachés, retrouvés par
labels `trainhub*`).

## Code tiers

Le thème du front reprend des tokens du système de thème d'OpenCode (licence MIT), voir
[`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES).
