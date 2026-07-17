FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

COPY packages/aios-core /app/packages/aios-core
RUN uv pip install --system /app/packages/aios-core

COPY services/gateway /app/services/gateway
RUN uv pip install --system /app/services/gateway

ENV PYTHONPATH=/app/services/gateway:/app/packages/aios-core

EXPOSE 8000

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
