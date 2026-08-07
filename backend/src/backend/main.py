from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import settings
from .db import get_session

app = FastAPI(title="budgeter")

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


app.include_router(api)
