# Machine GPU simulée pour tester le chemin SSH complet : sshd + client Docker.
# Le socket Docker de l'hôte est monté (Docker outside of Docker) : les conteneurs
# d'entraînement tournent sur le Docker de l'hôte.
FROM docker:27-cli AS dockercli

FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd \
 && useradd -m -s /bin/bash alice \
 && usermod -p '*' alice \
 && mkdir -p /home/alice/.ssh /home/alice/seg/configs \
 && chown -R alice:alice /home/alice \
 && printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nAllowTcpForwarding no\nX11Forwarding no\n' \
    > /etc/ssh/sshd_config.d/trainhub.conf
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY entrypoint.sh /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
