"""Memory service main app."""
from fastapi import FastAPI
from aios_core.config import settings
from aios_core.logging import configure_logging

configure_logging(settings.log_level, "memory")
app = FastAPI(title="AIOS Memory Service", version="0.1.0")

@app.get("/health")
async def health():
    return {"status": "ok", "service": "memory"}
