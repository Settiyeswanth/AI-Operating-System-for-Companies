FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

COPY packages/aios-core /app/packages/aios-core
RUN uv pip install --system /app/packages/aios-core

COPY services/memory /app/services/memory
COPY services/connectors /app/services/connectors
COPY services/ingestion /app/services/ingestion
RUN uv pip install --system /app/services/memory
RUN uv pip install --system /app/services/connectors
RUN uv pip install --system /app/services/ingestion

ENV PYTHONPATH=/app/services/ingestion:/app/services/memory:/app/services/connectors:/app/packages/aios-core
ENV LEDGER_PATH=/data/ledger/events.db
ENV ER_DB_PATH=/data/er/entity_resolution.db

VOLUME ["/data"]

CMD ["python", "-m", "ingestion.pipeline"]
