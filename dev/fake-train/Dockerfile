# Image de test TrainHub : Python léger, deux venvs (/opt/venvs/main et /opt/venvs/exp)
# et un train.py qui utilise réellement Hydra et tqdm.
FROM python:3.12-slim

ARG PIP_INDEX_URL
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1

RUN python -m venv /opt/venvs/main \
 && /opt/venvs/main/bin/pip install "hydra-core==1.3.2" "tqdm==4.66.5" \
 && python -m venv /opt/venvs/exp \
 && /opt/venvs/exp/bin/pip install "hydra-core==1.3.2" "tqdm==4.66.5" \
 && echo exp > /opt/venvs/exp/VARIANT && echo main > /opt/venvs/main/VARIANT

WORKDIR /workspace/fake
COPY train.py /workspace/fake/train.py
COPY configs /workspace/fake/configs

CMD ["/opt/venvs/main/bin/python", "train.py", "--config-name", "baseline"]
