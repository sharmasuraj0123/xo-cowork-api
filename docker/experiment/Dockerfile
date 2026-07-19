ARG BASE_IMAGE=agent-api-sandbox:latest
FROM ${BASE_IMAGE}

USER root

COPY requirements.txt /tmp/xo-experiment-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/xo-experiment-requirements.txt \
    && rm -f /tmp/xo-experiment-requirements.txt \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/sandbox sandbox \
    && mkdir -p /workspace /codex-home \
    && chown -R 10001:10001 /workspace /codex-home /home/sandbox

USER 10001:10001
ENV HOME=/home/sandbox \
    CODEX_HOME=/codex-home
WORKDIR /workspace

EXPOSE 5002
