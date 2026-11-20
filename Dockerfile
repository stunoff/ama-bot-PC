FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN --mount=type=secret,id=pip_ca,target=/run/secrets/pip_ca \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY --chown=10001:10001 ama_rs ./ama_rs
COPY --chown=10001:10001 migrations ./migrations
COPY --chown=10001:10001 alembic.ini .
USER 10001:10001
CMD ["python", "-m", "ama_rs"]
