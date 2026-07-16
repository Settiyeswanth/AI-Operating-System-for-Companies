FROM python:3.11-slim
WORKDIR /app
RUN pip install uv
COPY packages/aios-core /app/packages/aios-core
RUN uv pip install --system /app/packages/aios-core
COPY services/memory /app/services/memory
COPY services/agents /app/services/agents
RUN uv pip install --system /app/services/memory
RUN uv pip install --system /app/services/agents
ENV PYTHONPATH=/app/services/agents:/app/services/memory:/app/packages/aios-core
CMD ["python", "-m", "agents.main"]
