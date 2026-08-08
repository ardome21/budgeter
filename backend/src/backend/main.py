from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import PUBLIC_PATHS, get_user, session_user
from .config import settings
from .db import get_session
from .routers import (
    accounts,
    auth_routes,
    config,
    holdings,
    imports,
    merchants,
    passkeys,
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

# Signed, HTTP-only session cookie. HTTP-only means script on the page cannot
# read it, and SameSite=lax means another site cannot cause an authenticated
# request. Expiry is *also* checked server-side in session_user — a cookie's
# own max-age is a hint the client is free to ignore.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret.get_secret_value(),
    session_cookie="budgeter_session",
    max_age=settings.session_days * 24 * 60 * 60,
    same_site="lax",
    # Set only when actually served over TLS. Forcing it on plain http, which
    # is how this runs locally, means the browser silently drops the cookie and
    # every login appears to succeed and then fail.
    https_only=False,
)


def require_login(request: Request, db: Session = Depends(get_session)) -> None:
    """Refuse anything under /api without a session.

    Attached to the /api router itself rather than to each route, so the
    default is closed: a router added later is protected by virtue of being
    mounted, and every exception has to be written down in PUBLIC_PATHS where
    it can be read. The per-route alternative fails silently — a handler that
    forgets the dependency is simply open, and nothing about it looks wrong.
    """
    if request.url.path in PUBLIC_PATHS:
        return
    # Before setup has run there is no user and nothing to protect: the first
    # launch would otherwise need a login that cannot exist yet. Creating a
    # user closes this permanently, and /auth/setup refuses a second one.
    if get_user(db) is None:
        return
    if session_user(request, db) is None:
        raise HTTPException(401, "sign in to continue")


# Everything the backend serves lives under /api, so a dev proxy or a
# CloudFront behaviour can route the whole API with a single rule.
api = APIRouter(prefix="/api", dependencies=[Depends(require_login)])


@api.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@api.get("/health/db")
def health_db(session: Session = Depends(get_session)) -> dict[str, str]:
    """Confirms the Docker database is actually reachable."""
    session.execute(text("select 1"))
    return {"database": "ok"}


api.include_router(auth_routes.router)
api.include_router(passkeys.router)
api.include_router(views.router)
api.include_router(accounts.router)
api.include_router(config.router)
api.include_router(transactions.router)
api.include_router(merchants.router)
api.include_router(imports.router)
api.include_router(holdings.router)
api.include_router(plaid_link.router)

app.include_router(api)
