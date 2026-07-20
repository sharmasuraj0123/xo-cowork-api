ARG BASE_IMAGE=agent-api-sandbox:latest
FROM ${BASE_IMAGE}

USER root

COPY requirements.txt /tmp/xo-experiment-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/xo-experiment-requirements.txt \
    && rm -f /tmp/xo-experiment-requirements.txt \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/sandbox sandbox \
    && mkdir -p /workspace /codex-home /opt/xo-experiment \
    && chown -R 10001:10001 /workspace /codex-home /home/sandbox

COPY vps_server.py /opt/xo-experiment/vps_server.py

# The standalone Experiment profile is intentionally unrestricted inside the
# disposable container. Docker/VM isolation remains the outer boundary.
USER root
ENV HOME=/home/sandbox \
    CODEX_HOME=/codex-home \
    EXPERIMENT_CONTROL_PORT=8787
WORKDIR /workspace

EXPOSE 8787 5002 3000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=5s --timeout=2s --start-period=10s --retries=12 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8787/healthz >/dev/null || exit 1

CMD ["python", "/opt/xo-experiment/vps_server.py"]
