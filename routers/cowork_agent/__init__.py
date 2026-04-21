"""Router aggregation for the cowork_agent subpackage.

Each route module exposes a single `router: APIRouter`. `all_routers` is the
ordered list `server.py` uses to mount them onto the FastAPI app.

Migrated from bridge/routes/__init__.py on 2026-04-20. Bridge's health route
is intentionally not migrated (xo-cowork-api's existing /health stays).
"""

from fastapi import APIRouter

from .agents import router as agents_router
from .channels import router as channels_router
from .chat import router as chat_router
from .config import router as config_router
from .files import router as files_router
from .fts import router as fts_router
from .misc import router as misc_router
from .secrets import router as secrets_router
from .sessions import router as sessions_router
from .usage import router as usage_router
from .workspace_memory import router as workspace_memory_router

all_routers: list[APIRouter] = [
    sessions_router,
    chat_router,
    agents_router,
    config_router,
    channels_router,
    files_router,
    workspace_memory_router,
    secrets_router,
    usage_router,
    fts_router,
    misc_router,
]
