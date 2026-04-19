FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    PATH="/opt/poetry-venv/bin:$PATH"

WORKDIR /app

COPY cert.pem* /app/

RUN set -eu; \
    if [ -f /app/cert.pem ]; then \
        cp /app/cert.pem /usr/local/share/ca-certificates/code-agent-local.crt; \
        update-ca-certificates; \
    fi

COPY pyproject.toml poetry.lock /app/

RUN set -eu; \
    python -m venv /opt/poetry-venv && \
    pip install --no-cache-dir --upgrade pip==25.2 && \
    pip install --no-cache-dir "poetry>=2.0.1,<3.0" && \
    poetry config virtualenvs.create false && \
    poetry install --only main --no-root --no-interaction --no-ansi

COPY . /app

RUN poetry install --only main --no-interaction --no-ansi

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
