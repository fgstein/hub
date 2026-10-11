#!/usr/bin/env bash
# Génère la clé SSH de test des machines simulées et une config SSH dédiée.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p ssh
chmod 700 ssh
if [ ! -f ssh/id_ed25519 ]; then
  ssh-keygen -q -t ed25519 -N "" -C "trainhub-sim" -f ssh/id_ed25519
fi
cat > ssh/config <<CFG
# Généré par dev/gen-ssh-key.sh — à inclure dans ~/.ssh/config :
#   Include $(pwd)/ssh/config
Host sim-gpu-01
  HostName 127.0.0.1
  Port 2201
  User alice
  IdentityFile $(pwd)/ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
Host sim-gpu-02
  HostName 127.0.0.1
  Port 2202
  User alice
  IdentityFile $(pwd)/ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
CFG
chmod 600 ssh/config
echo "Clé : $(pwd)/ssh/id_ed25519"
echo "Ajoutez à ~/.ssh/config :  Include $(pwd)/ssh/config"
