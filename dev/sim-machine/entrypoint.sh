#!/bin/sh
# Installe la clé publique de test et donne à alice l'accès au socket Docker monté.
set -e
if [ -f /keys/id_ed25519.pub ]; then
  install -o alice -g alice -m 600 /keys/id_ed25519.pub /home/alice/.ssh/authorized_keys
fi
if [ -S /var/run/docker.sock ]; then
  gid=$(stat -c %g /var/run/docker.sock)
  group=$(getent group "$gid" | cut -d: -f1)
  if [ -z "$group" ]; then
    groupadd -g "$gid" dockerhost
    group=dockerhost
  fi
  usermod -aG "$group" alice
fi
# Configs Hydra « découvrables » par ls
for c in baseline long broken; do touch "/home/alice/seg/configs/$c.yaml"; done
chown -R alice:alice /home/alice/seg
ssh-keygen -A >/dev/null
exec /usr/sbin/sshd -D -e
