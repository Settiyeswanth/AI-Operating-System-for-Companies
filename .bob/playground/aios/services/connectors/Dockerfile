FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

COPY packages/aios-core /app/packages/aios-core
RUN uv pip install --system /app/packages/aios-core

COPY services/memory /app/services/memory
COPY services/connectors /app/services/connectors
RUN uv pip install --system /app/services/memory
RUN uv pip install --system /app/services/connectors

ENV PYTHONPATH=/app/services/connectors:/app/services/memory:/app/packages/aios-core

EXPOSE 8010

# Run the webhook receiver + polling scheduler
CMD ["python", "-m", "connectors.main"]
