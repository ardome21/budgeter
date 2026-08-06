# budgeter

Angular frontend · FastAPI backend · PostgreSQL in Docker · AWS CDK infrastructure.

One repo, one clone, one `docker compose up`. Nothing installs itself into your
laptop's boot sequence.

## Layout

```
frontend/   Angular 20 (npm)
backend/    FastAPI, Python 3.12 (uv)
infra/      AWS CDK, Python
docker-compose.yml   postgres:17 — runs only when you start it
```

## Prerequisites

Already installed on this machine: `node` (nvm 22.18), `uv`, `psql` (libpq),
`aws`, `cdk`, `gh`, Docker Desktop.

## Running it

**Database** — start first, stop when you're done for the day:

```sh
docker compose up -d db      # start
docker compose down          # stop (data persists in the volume)
docker compose down -v       # stop AND erase all data
```

**Backend** — http://localhost:8000, docs at `/docs`:

```sh
cd backend
uv run uvicorn backend.main:app --reload
```

**Frontend** — http://localhost:4200:

```sh
cd frontend
npm start
```

**Verify the whole chain** with the DB running:

```sh
curl localhost:8000/health/db      # {"database":"ok"}
```

## Database access

```sh
psql postgresql://budgeter:budgeter_dev@localhost:5432/budgeter
```

Credentials live in `docker-compose.yml` and are local-dev only. Real
credentials belong in `.env` (gitignored) or AWS Secrets Manager — never in
a committed file, and never on the Desktop.

## Infrastructure

```sh
cd infra
source .venv/bin/activate
cdk synth        # render CloudFormation
cdk diff         # compare against deployed
cdk deploy       # push to AWS
```

## Conventions

- Postgres runs in Docker, never as a native install.
- Python is pinned per-project via `.python-version` (3.12), not system Python.
- `uv add <pkg>` to add dependencies — it updates `pyproject.toml` and `uv.lock`.
