FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ENERGY_STATE_DIR=/var/lib/energy-agent \
    PATH=/opt/energy-agent-tools/.venv/bin:$PATH

RUN groupadd --system --gid 10001 energy \
    && useradd --system --uid 10001 --gid energy --create-home --home-dir /home/energy energy \
    && install --directory --owner=energy --group=energy --mode=0700 /var/lib/energy-agent \
    && install --directory --owner=energy --group=energy --mode=0755 /opt/energy-agent-tools

WORKDIR /opt/energy-agent-tools

COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
COPY scripts/deployment_smoke.py ./scripts/deployment_smoke.py

RUN python -m pip install --no-cache-dir uv==0.12.5 \
    && uv sync --frozen --no-dev --no-editable --no-cache \
    && .venv/bin/python -c "import energy_agent_tools, uvicorn; print(energy_agent_tools.__name__)"

USER energy
VOLUME ["/var/lib/energy-agent"]
EXPOSE 8765

# The host CLI accepts --bind-host for the container bridge. Compose publishes
# the port on loopback at the operator's machine boundary.
ENTRYPOINT ["energy-agent"]
CMD ["host", "--config", "/etc/energy-agent/config.json", "--state-dir", "/var/lib/energy-agent", "--bind-host", "0.0.0.0", "--port", "8765"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import socket; s=socket.create_connection(('127.0.0.1', 8765), 2); s.close()"
