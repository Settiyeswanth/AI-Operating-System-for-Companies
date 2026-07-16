FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

# Install aios-core package
COPY packages/aios-core /app/packages/aios-core
RUN uv pip install --system /app/packages/aios-core

# Install interface service
COPY services/interface /app/services/interface
COPY services/memory /app/services/memory
COPY services/agents /app/services/agents
RUN uv pip install --system /app/services/memory
RUN uv pip install --system /app/services/agents
RUN uv pip install --system /app/services/interface

ENV PYTHONPATH=/app/services/interface:/app/services/memory:/app/services/agents:/app/packages/aios-core

EXPOSE 8001

CMD ["uvicorn", "interface.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]
