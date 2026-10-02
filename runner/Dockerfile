FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp/runner
WORKDIR /opt/runner

COPY runner/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir pip==26.2.1 \
    && pip install --no-cache-dir --no-deps -r requirements.lock \
    && pip check \
    && groupadd --system --gid 10001 runner \
    && useradd --system --uid 10001 --gid runner --home-dir /tmp/runner runner \
    && mkdir -p /var/lib/agent-reach \
    && chown 10001:10001 /var/lib/agent-reach

COPY runner/agent_reach_runner ./agent_reach_runner

VOLUME ["/var/lib/agent-reach"]
EXPOSE 8443
USER 10001:10001
CMD ["python", "-m", "agent_reach_runner.server"]