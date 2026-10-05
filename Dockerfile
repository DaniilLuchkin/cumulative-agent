# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git bubblewrap util-linux ca-certificates && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.lock pyproject.toml ./
COPY src ./src
# Optional CA bundle secret for a managed build proxy; never copied into layers.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
COPY config/default.yaml ./config/default.yaml
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid 10001 --no-create-home agent && \
    mkdir -p /app/data && chown -R agent:agent /app/data
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
CMD ["cumulative-agent", "serve", "--host", "0.0.0.0"]
