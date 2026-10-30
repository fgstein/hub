# syntax=docker/dockerfile:1
# TrainHub image: built front end (Vite) + Python package + openssh-client, non-root user.

# --- 1. front end -------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- 2. Python package ----------------------------------------------------------------
FROM python:3.12-slim AS wheel
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=web /web/dist ./web/dist
RUN python -m build --wheel --outdir /dist

# --- 3. final image -------------------------------------------------------------------
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Non-root user; writable ~/.ssh (ControlMaster sockets).
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} th \
 && useradd -m -u ${UID} -g ${GID} -s /bin/bash th \
 && install -d -o th -g th -m 700 /home/th/.ssh \
 && install -d -o th -g th /var/lib/trainhub /etc/trainhub

COPY --from=wheel /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
COPY inventory.example.yaml /etc/trainhub/inventory.example.yaml

ENV TRAINHUB_INVENTORY=/etc/trainhub/inventory.yaml \
    TRAINHUB_DATA_DIR=/var/lib/trainhub \
    TRAINHUB_HOST=127.0.0.1 \
    TRAINHUB_PORT=8420 \
    TRAINHUB_EXECUTOR=ssh \
    PYTHONUNBUFFERED=1

USER th
WORKDIR /home/th
VOLUME ["/var/lib/trainhub"]
EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ[\"TRAINHUB_PORT\"]}/api/health', timeout=4)" || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["trainhub", "serve"]
