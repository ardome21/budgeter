# budgeter

Angular frontend · FastAPI backend · PostgreSQL in Docker · AWS CDK infrastructure.

One repo, one clone, one `docker compose up`. Nothing installs itself into your
laptop's boot sequence.

## Layout

```
frontend/   Angular 20 (npm)
backend/    FastAPI, Python 3.12 (uv)
infra/      AWS CDK, Python 3.12 (uv)
docker-compose.yml   postgres:17 — runs only when you start it
```

## Prerequisites

Already installed on this machine: `node` (nvm 22.18), `uv`, `psql` (libpq),
`aws`, `cdk`, `gh`, Docker Desktop.

## First run

Nothing starts without credentials. Create them once:

```sh
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # paste as POSTGRES_PASSWORD
```

## Running it

**Database** — start first, stop when you're done for the day:

```sh
docker compose up -d db      # start
docker compose down          # stop (data persists in the volume)
docker compose down -v       # stop AND erase all data
```

**Backend** — http://localhost:8000, API under `/api`, docs at `/docs`:

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
curl localhost:8000/api/health/db      # {"database":"ok"}
```

## Importing the Excel history

The app replaces the `Budget YYYY.xlsx` workbooks. A one-shot importer loads
them; it is a migration tool, not a product feature.

```sh
cd backend
uv run alembic upgrade head
uv run python scripts/import_workbook.py --reset ~/Desktop/Budgets/Budget\ 20*.xlsx
```

`--reset` wipes every table first, so the import can be re-run as the merchant
rules are tuned. Close the workbooks in Excel first — a `~$` lock file means
you'd import a stale snapshot.

The importer guesses nothing silently. It reconciles two ways and prints an
anomaly report:

1. **Every row lands** — each sheet's column-D sum must equal what was imported
2. **The right column was read** — each sheet's own rollup `Total` is compared too

A difference in (2) is not necessarily an import bug. Those rollups are `SUMIF`
over a hardcoded list of category names, so any category missing from that list
is invisible to the total — the workbook silently under-reports and the import
is the one telling the truth. Two confirmed cases:

| Sheet | Under-reports by | Cause |
|---|---|---|
| `2024 May Spending` | $28.94 | A charge typed `' Subscription'`, not `'Subscriptions'` |
| `2026 Monthly Fixed Costs` | $89.00 | Inner Peaks is category `Health`, absent from the rollup list |

The second makes `Monthly Overview`'s disposable income optimistic by exactly
$89/month.

### Committed vs flexible is a property of the transaction

Not of the category. `Self Care` holds an $89/month gym membership *and* 77
one-off purchases; every committed-looking category in the history has
discretionary rows in it. So `categories.kind` does not encode commitment — it
only distinguishes `SPENDING` from `SAVINGS` (moved, not spent) and `OTHER`.

Commitment comes from `transactions.is_recurring` and from the `fixed_costs`
list. Note that `is_recurring` is under-recorded in the source workbooks — rent
is flagged on only 3 of 19 rows — so the transaction-level committed figure is
currently a floor. Matching transactions against the fixed-cost list is what
makes it exact, and that is Phase 4.

### Rent includes bundled utilities

Internet and water are billed inside the rent charge, so `Rent 1474.68 +
Bundled-Utility 78.69 = 1553.37` — the Rent sheet total, and the amount that
arrives as a single `BILT CARD HOUSING` transaction. The importer files bundled
utilities under **Rent**, not Utilities; what each line is stays in its
`description`. Filing them under Utilities would understate the rent commitment
and inflate utilities by the same amount, matching neither the Rent sheet nor
how the money actually leaves the account.

## Migrations

Alembic owns the schema. Never `create_all()` — it only ever creates, so the
first column you add puts your laptop and every other database out of sync.

```sh
cd backend
uv run alembic revision --autogenerate -m "create transactions"   # write it
uv run alembic upgrade head                                       # apply it
uv run alembic downgrade -1                                       # undo one
uv run alembic current                                            # where am I
uv run alembic check                                              # models vs db
```

The database must be running for any of these — autogenerate diffs your models
against the live schema.

- **Models must be imported by `backend/src/backend/models.py`.** Autogenerate
  only sees what's registered on `Base.metadata`; a model that module never
  imports looks like a table it should *drop*.
- **Read every generated migration before applying it.** Autogenerate handles
  added and dropped tables and columns well. It cannot see a rename — it emits
  a drop plus an add, which throws the data away. Rewrite those as
  `op.alter_column(..., new_column_name=...)` by hand.
- The connection string comes from `backend.config.settings`, not `alembic.ini`,
  so `.env` applies here exactly as it does to the app.

## Credentials

One gitignored `.env` at the repo root holds the database credentials, read by
both `docker-compose.yml` and the backend. `.env.example` is the committed
template and holds no real values.

The password is stored once, as `POSTGRES_PASSWORD`. The backend assembles the
connection URL from the parts in `backend/config.py` rather than storing a
second pre-formatted copy, and wraps the password in pydantic's `SecretStr` so
it can't leak through a log line or an error page.

Nothing has a fallback credential. A missing `.env` fails at startup — compose
refuses to interpolate, and `Settings()` raises on the missing fields. That is
deliberate: a default password is a password that reaches production by
accident.

**Changing the password** has no effect on a database that already exists —
Postgres reads those variables only when initialising an empty data directory.
Either recreate the volume (destroys all data):

```sh
docker compose down -v && docker compose up -d db
```

...or change it in place and update `.env` to match:

```sh
docker exec -it budgeter-db psql -U budgeter -c "\password budgeter"
```

**Production** credentials belong in AWS Secrets Manager, injected as
environment variables by the CDK stack. Never in a committed file, never on the
Desktop, and not in this `.env` either — this file is local-dev only.

## Database access

```sh
set -a; . ./.env; set +a
psql "postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@localhost:5432/$POSTGRES_DB"
```

The container publishes to `127.0.0.1:5432`, not `0.0.0.0` — the database is
reachable from this machine only, never from the network you're joined to.

## Infrastructure

```sh
cd infra
uv run cdk synth        # render CloudFormation
uv run cdk diff         # compare against deployed
uv run cdk deploy       # push to AWS
uv run pytest           # stack assertions
```

The stack is currently environment-agnostic — `env=` is commented out in
`app.py`. Uncomment it before using anything that needs a context lookup
(VPCs, hosted zones, real AZ enumeration).

## Conventions

- Postgres runs in Docker, never as a native install, published to loopback only.
- Secrets live in `.env` (gitignored) with a committed `.env.example` template.
  No credential, anywhere, has a default value.
- Python is pinned per-project via `.python-version` (3.12), not system Python.
- `uv` manages both `backend/` and `infra/` — `uv add <pkg>` updates
  `pyproject.toml` and `uv.lock`. No `requirements.txt` anywhere.
- One `.gitignore`, at the repo root.
- Everything the API serves lives under `/api`.
- **Keep this repo out of iCloud** (`~/Documents`, `~/Desktop`). iCloud sets the
  macOS `UF_HIDDEN` flag on files it manages, and CPython's `site.addpackage()`
  silently skips hidden `.pth` files. That kills the editable install and you get
  `ModuleNotFoundError: No module named 'backend'` with nothing in the traceback
  pointing at the cause. Diagnose with `ls -lO .venv/lib/python3.12/site-packages/*.pth`
  — a `hidden` flag in that column is the tell. It lives in `~/Coding` for this reason.
