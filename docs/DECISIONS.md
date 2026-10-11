# Choix d'implémentation et écarts par rapport à la spécification

Spécification : issue #1 (« TrainHub — lanceur d'entraînements Hydra »). Ce document liste,
étape par étape, ce qui a été fait, les choix pris seul quand la spec laissait une marge, et les
écarts. Les points marqués **[à valider]** méritent une confirmation.

## Points ouverts de la spec (section 12)

| Point | Décision appliquée |
|---|---|
| Choix des GPU | Champ facultatif `CUDA_VISIBLE_DEVICES` par run (`-e CUDA_VISIBLE_DEVICES=…`), dans le brouillon (« GPU »), l'API (`cuda_visible_devices`), le MCP et la CLI (`--gpus`). Valeur validée (`0,1`, `all`, `none`). |
| Overrides Hydra | Pas de champ dans le brouillon (défaut de la spec). Le cœur, l'API, le MCP (`overrides`) et la CLI (arguments positionnels) les acceptent, chacun échappé. |
| Code à jour | Monté tel quel (déjà présent sur la machine) : rien n'est fait avant le lancement. |
| Multi-utilisateur | Mono-utilisateur, jeton optionnel (`TRAINHUB_TOKEN`). |
| Nettoyage | Pas de suppression automatique ; suppression manuelle. |
| Couleur de `stopped` | **Gris** (pastille fixe avec ■), pour distinguer un arrêt volontaire d'un échec. |
| Notifications | Aucune notification navigateur. |

## Étape 1 — socle et cœur

- `core/commands.py` : les fonctions `docker_*` renvoient l'argv **distant** ; `transport()`
  l'enveloppe dans `ssh … -- '<commande distante>'` (un seul argument, `shlex.join`), sauf pour
  une machine `localhost` (sans `user` ni `port`). Les fonctions `build_*` de la spec renvoient
  l'argv complet.
- Labels supplémentaires sur les conteneurs : `trainhub.machine`, `trainhub.image`,
  `trainhub.venv`, `trainhub.config` (en plus de `trainhub`, `trainhub.run_id`,
  `trainhub.project`). Ils permettent d'importer un conteneur inconnu avec tous ses paramètres et
  de distinguer les « machines » qui partagent le même Docker (mode `local`).
- `-w <workdir>` est passé à `docker run` en plus du `cd` dans le script (comme dans l'exemple).
- `preview()` renvoie la forme multi-ligne lisible de la spec (un seul niveau d'échappement) ;
  `preview_exact()` renvoie la ligne exacte copiable (utilisée par le bouton « copier »).
- Placeholders : `{nom}` non précédé de `$`, pour laisser passer `${VAR}` et les interpolations
  Hydra `${oc.env:X}`. Un placeholder inconnu est une erreur de validation.
- Tests d'échappement : exécution réelle des deux niveaux de shell (`sh -c` pour le shell
  distant, `bash -c` pour le conteneur) avec espaces, `'`, `"`, `$`, `;`, `` ` ``, `$(…)`, `*`,
  `\`, unicode et les overrides Hydra de la spec.
- Configuration par variables d'environnement : `config.py`. Défaut de `TRAINHUB_DATA_DIR` :
  `./.trainhub` (la spec ne fixe pas de défaut ; `/var/lib/trainhub` dans l'image Docker).

## Étape 2 — exécuteurs et service

- Interface `Executor` : `exec(machine, argv_distant, timeout)`, `stream(machine, argv)` et
  `full_argv()` (audit). Délais : 10 s par action, 5 s pour le suivi, 600 s pour `docker run`
  (qui peut télécharger l'image), `stop_timeout + 10 s` pour `docker stop`.
- `ssh` : options `BatchMode=yes` et `ConnectTimeout=5` ajoutées (jamais de prompt interactif) ;
  code 255 ou délai dépassé = machine injoignable.
- Exécuteur `fake` : il interprète l'argv docker réellement construit (ce qui teste aussi la
  construction des commandes). Une image dont le nom contient `missing` simule une image absente.
- Un arrêt impossible (machine injoignable) est abandonné proprement (`stop_requested` remis à
  faux) : au retour de la machine le run redevient `running` et peut être arrêté à nouveau.
- `stopped` vs `failed` : un champ interne `stop_requested` (persisté) indique qu'un arrêt vient
  de TrainHub. Champ interne `paused_at` pour le calcul de `paused_total_s`.
- Un run en base dont le conteneur a disparu passe en `failed` (« conteneur introuvable… »).
- **Relancer** crée un run avec les mêmes paramètres et la **commande actuelle** du projet
  (pas l'ancienne commande résolue). **[à valider]**
- **Redémarrer** (`docker start`) remet à zéro `started_at`, la durée de pause et le code de
  sortie ; les logs du conteneur (qui contiennent les deux exécutions) sont rechargés.
- Hub de logs : quand le flux redémarre (relance, retour d'une machine), un message `reset` vide
  les terminaux avant de renvoyer le contenu (évite les doublons).
- Le statut `unreachable` n'est appliqué qu'aux runs actifs ; un run en `launching` reste géré
  par son lancement (qui échoue avec un message explicite si la machine est injoignable).
- Image de test `dev/fake-train` : Hydra 1.3.2 + tqdm, deux venvs, configs `baseline`, `long`,
  `broken` (`fail_at_epoch: 2`), gestion de SIGTERM (code 143).

## Étape 3 — API et WebSocket

- Routes conformes à la spec. Ajouts : `GET /api/runs?status=active|finished` (alias),
  `refresh=true` sur les configs, `GET /docs` (Swagger) et `/openapi.json`.
- Codes d'erreur : 404 (inconnu), 409 (action interdite pour ce statut, action déjà en cours,
  machine injoignable), 422 (validation). Corps `{"detail": "<message lisible>"}`.
- WebSocket : en-tête binaire de 8 octets = id du run en ASCII complété par des espaces.
  Messages JSON supplémentaires : `logs.reset`, `logs.end`, `ui_state.updated`, `pong`, `error`.
  Le client ignore les événements de séquence ≤ celle du dernier snapshot ; à la reconnexion le
  serveur renvoie automatiquement un snapshot complet.
- Types TypeScript générés par `openapi-typescript` (`make openapi`) ; les messages WebSocket,
  absents de l'OpenAPI, sont typés à la main (`web/src/lib/types.ts`).

## Étapes 4-5 — front

- Thème : tokens de couleur/typographie d'OpenCode (sombre par défaut, clair, ou « système » qui
  suit `prefers-color-scheme` ; bascule par le bouton en haut à droite, mémorisée localement).
- Le thème et les composants ont été réécrits avec Tailwind v4 + Kobalte (pas de copie des
  composants OpenCode, qui dépendent de leur propre système). Seuls les tokens sont repris
  (voir `THIRD_PARTY_NOTICES`).
- Linter front : **Biome** (lint + format) plutôt qu'ESLint (la spec autorise les deux).
- Raccourcis : `Ctrl+Alt+←/→` (onglets), `N` (nouveau brouillon), `Entrée` (lancer, hors liste
  ouverte), `Échap` (annuler le brouillon, quitter le plein écran).
- Les brouillons sont propres à chaque navigateur (non persistés) ; la dernière sélection par
  projet est persistée côté serveur et sert de valeur initiale.
- L'aperçu de commande utilise la sélection du premier brouillon complet, sinon la dernière
  sélection du projet.
- Le bouton « + » des brouillons est toujours visible sous les runs actifs ; l'état vide affiche
  un bouton « Nouveau run » mis en avant.
- Un terminal déplié reste ouvert quand son run passe dans « Terminés » (état mémorisé par run).
- Un run jamais démarré (échec au lancement) affiche « — » comme durée.
- Bundle principal ≈ 72 ko gzip ; xterm et ses addons sont chargés à la première ouverture d'un
  terminal (chunk séparé). Vérifié en CI (< 300 ko).

## Étape 6 — MCP

- **SDK `mcp` épinglé en `<2`** : la version 2 a renommé `FastMCP` en `MCPServer` avec des
  changements d'API ; la spec demande FastMCP. Migration à prévoir plus tard. **[à valider]**
- Paramètres facultatifs ajoutés à `launch_run`/`preview_command` : `overrides`,
  `cuda_visible_devices`. `launch_run` et `stop_run` attendent jusqu'à 15 s le résultat.
- `get_logs` : 200 lignes par défaut, `grep` = regex insensible à la casse (texte brut si regex
  invalide), sortie tronquée à 12 000 caractères avec mention.
- La protection DNS rebinding du SDK est désactivée : TrainHub écoute sur 127.0.0.1 par défaut
  et la sécurité réseau passe par le jeton.

## Étape 7 — SSH et déploiement

- Machines simulées : base `ubuntu:24.04` + `openssh-server`, client Docker statique copié
  depuis `docker:27-cli`, socket Docker de l'hôte monté. Utilisateur `alice`, configs
  découvrables dans `/home/alice/seg/configs`. Ports 2201/2202 sur 127.0.0.1.
- Dockerfile : `node:22-slim` (build du front), `python:3.12-slim` (wheel puis image finale avec
  `openssh-client` et `tini`). Utilisateur `th` (UID 1000, configurable), `~/.ssh` inscriptible
  pour les sockets ControlMaster, `HEALTHCHECK` sur `/api/health`.
- Logs du serveur en JSON (une ligne par événement), uvicorn compris ; chaque commande exécutée
  est journalisée avec sa durée et son code de retour (`docker ps` au niveau DEBUG pour éviter le
  bruit, le reste au niveau INFO).
- Licence du projet non choisie (à décider par le propriétaire du dépôt).

## Vérifications effectuées dans l'environnement de développement

- `pytest` : cœur, service (fake), API/WebSocket, MCP (mémoire, HTTP, stdio), exécuteur SSH
  (faux binaire), intégration Docker locale réelle (image `trainhub-fake-train`) et chemin SSH
  complet réel vers les machines simulées (y compris coupure puis retour d'une machine).
- `vitest` (formatage, stores) et Playwright (brouillon → lancement → terminal → plein écran →
  pause/reprise → arrêt, échec sur machine injoignable, commande, onglets, largeur 900 px).
- Image Docker construite et démarrée en mode fake (utilisateur non root, front servi). Dans le
  bac à sable, l'accès réseau des builds a nécessité des réglages locaux (proxy, miroir) non
  inclus dans le dépôt.

## Évolution — choix de la config dans l'arborescence (demande du 11/10)

Demande : la config est le nom d'un fichier YAML Hydra ; la racine des fichiers est donnée par
projet ; l'utilisateur choisit en cliquant dans l'arborescence complète (récursive) ; chaque
config indique combien de runs ont déjà été lancés dessus.

- Inventaire : `configs.root` (forme courte de `configs.discover.path`), `pattern` (défaut
  `*.yaml`), `exclude` (globs de chemins relatifs, ex. `hydra/*`), `max_depth` (défaut 10).
  `root: ~/x` est relatif au home de la session SSH.
- Découverte : `find -L <root> -maxdepth N -type f -name <pattern>` sur la machine choisie
  (au lieu de `ls`), cache 60 s, bouton ↻. Composants cachés ignorés. Un sous-dossier illisible
  n'empêche pas d'afficher le reste.
- **Identifiant d'une config = chemin relatif sans extension** (`experiments/unet_large`), et
  non le seul basename : deux fichiers de même nom dans deux dossiers restent distincts, et
  c'est la valeur que Hydra accepte pour `--config-name` (config_path = racine). Le basename
  reste disponible via le nouveau placeholder `{config_name}` (ex. `--config-path …
  --config-name {config_name}`). **[à valider]**
- Compteurs : calculés en direct côté front depuis l'état des runs (tous statuts, et en cours)
  par fichier et cumulés par dossier ; côté serveur, `run_counts` dans
  `GET /api/projects/{id}/configs` et dans l'outil MCP `list_configs`. Un run supprimé ne compte
  plus.
- Sélecteur : popover avec recherche (filtre sur le chemin, dossiers parents conservés), arbre
  repliable (tout déplié par défaut), navigation clavier (↑/↓, →/← pour déplier/replier ou
  remonter au dossier parent, Entrée), racine affichée en tête, légende en pied.
