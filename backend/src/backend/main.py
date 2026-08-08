from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import settings
from .db import get_session
from .routers import (
    accounts,
    config,
    imports,
    merchants,
    plaid_link,
    transactions,
    views,
)

app = FastAPI(title="budgeter")

# The dev server proxies /api to this process, so the browser sees one origin
# and CORS is not what makes the app work. Kept as a safety net for direct
# calls to :8000.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Everything the backend serves lives under /api, so a dev proxy or a
# CloudFront behaviour can route the whole API with a single rule.
api = APIRouter(prefix="/api")


@api.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@api.get("/health/db")
def health_db(session: Session = Depends(get_session)) -> dict[str, str]:
    """Confirms the Docker database is actually reachable."""
    session.execute(text("select 1"))
    return {"database": "ok"}


api.include_router(views.router)
api.include_router(accounts.router)
api.include_router(config.router)
api.include_router(transactions.router)
api.include_router(merchants.router)
api.include_router(imports.router)
api.include_router(plaid_link.router)

app.include_router(api)
