"""FastAPI app: control-plane API for the AiTeam web UI.

Run with (from repo root):
    PYTHONPATH=src .venv/bin/uvicorn server.app:app --reload --port 8000
"""

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


app = FastAPI(title="AiTeam API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router)
app.include_router(runs_routes.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
