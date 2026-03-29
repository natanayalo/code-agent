FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

COPY . /app

RUN set -eu; \
    if [ -f /app/cert.pem ]; then \
        cp /app/cert.pem /usr/local/share/ca-certificates/code-agent-local.crt; \
        update-ca-certificates; \
    fi; \
    python -m pip install --upgrade pip==25.2 && pip install .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
