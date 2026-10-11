#!/bin/sh
# Installs the test public key and gives alice access to the mounted Docker socket.
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
# The Hydra configs of the test image are mounted in /home/alice/seg/configs.
ssh-keygen -A >/dev/null
exec /usr/sbin/sshd -D -e
