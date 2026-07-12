"""FastAPI app: control-plane API for the Looper web UI.

Run with (from repo root):
    PYTHONPATH=src .venv/bin/uvicorn server.app:app --reload --port 8000
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import init_db
from .routes import auth as auth_routes
from .routes import runs as runs_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Looper API", lifespan=lifespan)

# Default covers the Vite dev server; deployments (deploy/docker-compose.yml
# serves the built frontend on a different origin) override via
# LOOPER_CORS_ORIGINS, comma-separated.
_cors_origins = [
    o.strip()
    for o in os.environ.get("LOOPER_CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router)
app.include_router(runs_routes.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
