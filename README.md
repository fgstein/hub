# TrainHub

TrainHub lance et suit des entraînements [Hydra](https://hydra.cc) sur des machines distantes,
depuis une interface web, un serveur MCP (pour les agents) ou en Python pur.

Chaque run est construit à partir de 4 choix (**machine**, **image Docker**, **venv**, **config**)
et exécute la chaîne :

```
ssh machine → docker run -d → source venv → cd workdir → python train.py --config-name XXX
```

Quand la commande se termine, le conteneur **reste démarré** (statut du run : réussi ou échoué
selon le code de sortie) : on peut y ouvrir un shell depuis l'interface, puis le fermer soi-même
(« Fermer le conteneur »). Désactivable par projet avec `docker.keep_alive: false`.

- Projets en onglets, chacun avec sa commande générique éditable. Placeholders remplis par
  TrainHub : `{config}`, `{config_name}`, `{run_id}` (identifiant généré au lancement, ex.
  `rn3ceg`), `{machine}`, `{project}`. **Tout autre `{nom}` est un paramètre du run** : le
  brouillon affiche un champ par paramètre (ex. `python train.py --config-name {config} {options}
  --my_option {option2}`). La valeur est découpée comme par un shell (`a=1 b=2` = deux arguments,
  `"a b"` = un seul), chaque mot est échappé, un champ vide n'insère rien ; les dernières valeurs
  sont mémorisées par projet.
- Venv au choix parmi ceux de l'inventaire, plus **`system`**, toujours proposé : aucun venv
  activé (Python de l'image, ou `uv run …` dans la commande).
- Suivi en direct : statut, temps écoulé, logs en streaming dans un terminal xterm.js (couleurs ANSI, barres tqdm).
- Logs de **tous** les runs sauvegardés au fil de l'eau dans `$TRAINHUB_DATA_DIR/logs/<run>.log` :
  toujours affichés et téléchargeables après la fermeture ou la suppression du conteneur, une
  coupure de la machine ou un redémarrage de TrainHub.
- Commande de chaque run visible dans son panneau : générique résolue, ou complète (ssh +
  `docker run` + script, options Docker du projet comprises).
- Shell interactif dans le conteneur d'un run (`docker exec -it`, venv activé, dans le workdir),
  onglet « shell » du panneau.
- Overrides Hydra en fin de commande via l'API, le MCP et la CLI (`trainhub launch … trainer.max_epochs=5`).
  Dans l'interface, on utilise un paramètre (`… {options}`) ; le champ « overrides » n'apparaît
  que pour relancer un run qui en avait.
- Contrôle : pause, reprise, arrêt, redémarrage, relance (ouvre un brouillon pré-rempli),
  fermeture du conteneur conservé, suppression.
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
      root: /home/alice/seg/configs   # racine des configs Hydra sur la machine (côté hôte)
      exclude: ["hydra/*"]            # optionnel
```

**Choix de la config.** Tous les fichiers `*.yaml` sous la racine du projet (récursivement,
via `find` sur la machine choisie, cache 60 s, bouton ↻) sont présentés sous forme
d'arborescence dans le brouillon : on clique sur le fichier voulu. Chaque fichier (et chaque
dossier, en cumulé) indique le nombre de runs déjà lancés sur cette config, avec un point vert
s'il y en a en cours. Une config est le chemin relatif sans extension
(`experiments/unet_large`) : c'est ce qu'attend `--config-name` de Hydra quand `config_path`
est la racine (une config principale dans un sous-dossier doit commencer par
`# @package _global_`). Le placeholder `{config_name}` donne le seul basename (`unet_large`).
Les dossiers et fichiers cachés (`.hydra`, `.git`…) sont ignorés.

L'état modifiable depuis l'interface (commande surchargée d'un projet, onglets ouverts, dernières
sélections, runs) vit dans une base SQLite (`$TRAINHUB_DATA_DIR/trainhub.sqlite`).

**Conteneur conservé et logs.** Avec `keep_alive` (défaut), le script lancé dans le conteneur
exécute la commande en arrière-plan, relaie `SIGTERM` (« Arrêter » = `docker stop`, comme
avant), puis, quand la commande se termine, écrit son code dans `/tmp/.trainhub-exit` et garde
le conteneur démarré. Un healthcheck (`--health-cmd`, toutes les 3 s) passe alors à
`unhealthy` : c'est ainsi que TrainHub détecte la fin et lit le code de sortie, sans commande
supplémentaire. Fermer le conteneur le fait sortir avec le code de la commande. Les logs sont
suivis en permanence pour chaque run actif et écrits dans `$TRAINHUB_DATA_DIR/logs/<run>.log`
(`<run>.done` quand la sauvegarde est complète) ; si les logs Docker repartent plus courts que la
sauvegarde (rotation), l'ancienne version est gardée en `<run>.prev.log`. Supprimer un run
conserve sa sauvegarde.

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
`relaunch_run`, `close_container`, `delete_run`, `set_project_command`) appellent la même couche service que
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
