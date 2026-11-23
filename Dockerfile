# Small, boring image: slim Python, non-root user, package installed (not editable), no build tools left behind.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOSTOPS_AUDIT_PATH=/data/audit.jsonl

WORKDIR /app
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app /data

# Dependencies first so code changes don't bust the dependency layer.
COPY pyproject.toml ./
COPY hostops ./hostops
RUN pip install ".[llm]"

USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["uvicorn", "hostops.service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
