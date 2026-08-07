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
curl localhost:4200/api/health/db      # same, through the dev proxy
```

The second one matters: the browser only ever talks to `/api` on its own origin,
which `frontend/proxy.conf.json` forwards to port 8000. CORS is configured but is
not what makes the app work — it is a safety net for direct calls to `:8000`.

## The app

Four screens, all reading and writing the same database the workbook was
imported into.

| Screen | What it replaces |
|---|---|
| **Month** | The `*Budget` sheets — allocations vs actuals, committed/flexible, pace |
| **Transactions** | The `*Spending` sheets — list, add by hand, recategorise inline |
| **Import** | Pasting a bank export into a sheet, then categorising it by hand |
| **Merchants** | Nothing. A workbench for what each merchant costs, plus the consolidation the spreadsheet had no way to do |
| **Accounts** | The `Accounts` sheet — net worth over time, and recording a new snapshot |
| **Settings** | The `Monthly Fixed Costs`, `Paycheck` and `Rent` sheets, plus reconciliation |

### Config is editable, and keeps its history

Fixed costs and paycheck lines carry `effective_from` / `effective_to`.
Changing an amount ends the current row and opens a new one, so last year's rent
stays answerable; changing a description edits in place, because fixing a typo
is not a price change. Deleting ends a commitment rather than erasing it — a
cancelled subscription still explains last month.

A bill can carry its own breakdown. Rent arrives as one charge but is really
thirteen lines, and the components sum to exactly the parent, never counted
beside it.

### Reconciliation

What each commitment expected against what actually left the account. The
workbook had rent expected at 1553.37 on one sheet and BILT CARD HOUSING
charging 1535.17 on another, with nothing connecting them.

A commitment is matched to a merchant explicitly, because guessing from the
description gets Netflix right and rent wrong — the bill is called "Rent" and
the charge says BILT CARD HOUSING. Unmatched commitments say so and offer
candidates rather than falling back to a category total, which made Energy and
Phone both report the entire Utilities figure.

The link is worth setting for every commitment. Seven were pointing at names
nobody bills under — the workbook typed `Phone` and `NYT` by hand for three
years while the bank writes `Spectrum Mobile` and `NYTIMES*` — and a commitment
linked to a dead name reports "expected but not charged this month" forever,
which reads like a missed payment and is not one. Linked, July 2026 reconciles
15 of 16, and the rent drift the workbook could not see is on screen: 1553.37
expected against 1535.17 charged.

`iCloud` is deliberately left unlinked. It bills as `APPLE.COM/BILL`, the same
descriptor as Apple TV, so both resolve to one merchant and no link can tell
0.99 from 12.99. Apple TV therefore carries a 0.99 drift that is really the
iCloud charge. An unmatched row that says so beats a link that quietly reports
one subscription's cost as another's.

### Net worth

The `Accounts` sheet stored balances wide, a new column per snapshot, which is
why there were only eight in two years. As rows they are unbounded.

The chart is scaled by **actual date**, not by evenly spaced snapshots, so a
21-month gap looks like 21 months. Stretches with no reading are drawn dashed:
the line there is drawn, not measured. Series colours come from a categorical
palette validated against this app's own light and dark surfaces, and every
value is also in a table below the chart — a tooltip is never the only way to
read a number.

Net worth counts what you owe. The workbook's own `Total` row omitted the
student loan and the credit card balance, reporting 53,742.84 for March 2024
against a real 47,742.84.

### The merchant workbench

`/merchants` lists every merchant with what it cost, how often, when it was last
seen and its category mix — sorted by spend, because 277 of the 401 merchants
were seen exactly once and an alphabetical list buries the handful that matter.
Names are editable inline.

It also allows **hand-merging any selection**, which exists because the
suggestion rule keys on the first word and therefore can never propose
`Airbnb`, `Future Rent Airbnb` and `Revolution Park Air Bnb` — $6,599 split
across three records. Those have to be picked.

`/merchants/review` is the suggestion queue, linked from the workbench whenever
it has items.

### The merchant review queue

Normalization is conservative and under-merges on purpose: wrongly merging two
shops silently corrupts every total they appear in and cannot be undone, while
leaving one shop split is a click to fix.

The one thing normalization must not do is discard the name. It strips the
opaque reference a bank appends — `APPLE.COM/BILL`'s `CAMMGGH21Q0DA0` — by
looking for **a long run containing a digit**, because that is what separates
an identifier from a word. Matching on length alone ate the merchant instead:
descriptors arrive in capitals, so `DUKEENERGY BILL PAY 910175813041` became
`bill pay`, which is one typo from `BILT CARD HOUSING`, and the power bill and
the rent were duly proposed as one merchant and merged. Four other names —
`CHARLOTTE OBSERVER`, `POTBELLY SANDWICH`, `GRANDFATHER MOUNTAIN`,
`GUESTRS*BELLAGIO` — normalized to nothing at all and their charges got no
merchant, which is why the Observer subscription reconciled against an empty
month while the charge sat one table away.

A proposal is built from one rule — **two merchants are proposed when their
first word is the same brand**, allowing one character of typo. In a bank
descriptor the first token is the shop and everything after it is a branch, a
service or noise. So a proposal asks *"these share a brand, are they one
place?"*, which is a question with a real "no": `Uber Eats` is Food and Drinks
and `Uber Trip` is Transportation.

Because of that, members are ticked individually rather than accepted as a
group, and each proposal shows the **raw descriptors** behind every merchant —
`Rhino Mart` and `Rhino Market Deli` are indistinguishable as names, but what
the bank actually wrote makes the call obvious.

The surviving merchant gets **whatever name you type** — the normalized key is
a machine's guess (`Rhino Market Deli`), and `Rhino Market & Deli` is probably
what you actually want to see. Renaming rewrites the split records that
reference the old name, and merging deletes the folded name's records, so a
decision never ends up attached to a name that no longer exists.

Every answer is recorded in `merchant_splits`, so a rejected pair is never
proposed again. A review queue that cannot be emptied is one nobody works
through. After a partial merge only the pairs *against the surviving name* are
recorded — nothing has been decided about whether the leftovers match each
other.

### Money is a string on the wire

Every money field crosses the API as a JSON **string**, never a number.
JavaScript has no decimal type, so a JSON number is a float the moment it is
parsed. The browser formats what it is handed and never sums anything — every
total on screen was computed in Postgres.

### Importing a bank export

Preview first, commit second; nothing is written until the preview comes back
and you confirm it. Columns are detected across the usual header spellings,
amounts parse from `$1,234.56` / `(45.00)` / `-45`, and there is a toggle for
banks that export purchases as negative.

Merchants resolve against the three years of history already imported, so most
rows arrive with the right category already filled in.

A merchant does not *have* a category, it has a **history** of categories.
Rhino Market & Deli is Food and Drinks on a sandwich run and Groceries on a
shop, and both are right. So the preview suggests the most-used one and offers
every other category that merchant has genuinely been filed under as a
one-click chip, each showing how often.

There is no stored default to drift from that history — the column that held
one is gone. It recorded whichever transaction created the merchant, never
followed a merge, and ended up contradicting the merchant's own history for
twelve of them. Re-dropping the same file
is a no-op: CSV rows carry a content hash, and rows already present come back
ticked off as duplicates. **Hand-entered rows are never hashed and never
deduplicated** — two identical charges at the same bar on one night are usually
two real rounds.

## Importing the Excel history

**The database is the source of truth.** The workbooks are the three years of
history it was built from, and corrections now land as migrations rather than
as re-imports. The importer below is kept because it documents where every
figure came from, not because it is the way to change anything.

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
$89/month. Both are correct in the database: the misspelling is aliased to
Subscriptions, and the gym is a fixed cost inside the $2,032.90 monthly total.

Two further defects live only in the workbook and are not carried across. Its
`July Budget` sheet reads `Paycheck!F2*2` for "Taxes" and `Paycheck!F4*2` for
"Automatic Savings" — the insurance total and the tax total respectively. The
app derives both from `paycheck_lines` and is unaffected.

### Committed vs flexible is a property of the transaction

Not of the category. `Self Care` holds an $89/month gym membership *and* 77
one-off purchases; every committed-looking category in the history has
discretionary rows in it. So `categories.kind` does not encode commitment — it
only distinguishes `SPENDING` from `SAVINGS` (moved, not spent) and `OTHER`.

Commitment comes from `transactions.is_recurring` and from the `fixed_costs`
list. `is_recurring` is under-recorded in the source workbooks — rent is
flagged on only 3 of 19 rows — because the 'Automatic?' column was filled in by
hand and often not at all. `scripts/backfill_recurring.py` closes the gap by
marking anything charged by a merchant a standing commitment names:

```sh
uv run python scripts/backfill_recurring.py            # report only
uv run python scripts/backfill_recurring.py --apply    # write it
```

It is a dry run by default, because inference over a thousand rows changes how
every month's committed figure reads and should be looked at first. It follows
the **explicit merchant link** where a commitment has one, so it is worth
re-running after linking a commitment on the Settings screen — before the
links existed it found nothing for rent, phone or the paper, which are the
commitments the split most depends on.

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
