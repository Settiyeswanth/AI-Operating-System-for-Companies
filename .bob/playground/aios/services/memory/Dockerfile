FROM python:3.11-slim
WORKDIR /app
RUN pip install uv
COPY packages/aios-core /app/packages/aios-core
RUN uv pip install --system /app/packages/aios-core
COPY services/memory /app/services/memory
RUN uv pip install --system /app/services/memory
ENV PYTHONPATH=/app/services/memory:/app/packages/aios-core
VOLUME ["/data"]
EXPOSE 8002
CMD ["uvicorn", "memory.main:app", "--host", "0.0.0.0", "--port", "8002"]
