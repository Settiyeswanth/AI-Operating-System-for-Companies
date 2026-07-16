FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

# Install shared core package
COPY packages/aios-core /app/packages/aios-core
RUN uv pip install --system /app/packages/aios-core

# Install memory package (for GraphClient, VectorClient, EventLedger)
COPY services/memory /app/services/memory
RUN uv pip install --system /app/services/memory

# Install enrichment service
COPY services/enrichment /app/services/enrichment
RUN uv pip install --system /app/services/enrichment

ENV PYTHONPATH=/app/services/enrichment:/app/services/memory:/app/packages/aios-core
ENV LEDGER_PATH=/data/ledger/events.db

VOLUME ["/data"]

CMD ["python", "-m", "enrichment.pipeline"]
