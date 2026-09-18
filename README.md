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

```sh
make dev        # database, then backend and frontend together — Ctrl-C stops both
make            # list every target
```

Three processes, one per pane if you'd rather watch them separately. `make api`
and `make dev` start the database first, because the backend is useless without
it and `docker compose up` is a no-op when it's already running.

| | Runs | Longhand |
|---|---|---|
| `make db` | postgres, waited on until it accepts connections | `docker compose up -d --wait db` |
| `make db-stop` | stops it; data persists in the volume | `docker compose down` |
| `make db-reset` | stops it **and erases all data** | `docker compose down -v` |
| `make api` | http://localhost:8000, API under `/api`, docs at `/docs` | `cd backend && uv run uvicorn backend.main:app --reload` |
| `make web` | http://localhost:4200 | `cd frontend && npm start` |
| `make test` | the backend tests | `cd backend && uv run pytest` |
| `make test-web` | the frontend tests, once, headless | `cd frontend && npx ng test --watch=false --browsers=ChromeHeadless` |
| `make lint` | ruff over the backend | `cd backend && uv run ruff check .` |

The longhand column is the point of the table: the `Makefile` is shorthand for
commands that still work typed out, not a build system with rules of its own.
Nothing else in the repo depends on `make`.

**Verify the whole chain** with the DB running:

```sh
curl localhost:8000/api/health/db      # {"database":"ok"}
curl localhost:4200/api/health/db      # same, through the dev proxy
```

The second one matters: the browser only ever talks to `/api` on its own origin,
which `frontend/proxy.conf.json` forwards to port 8000. CORS is configured but is
not what makes the app work — it is a safety net for direct calls to `:8000`.
## The app

Five screens over one database that fills itself from the banks.

| Screen | What it answers |
|---|---|
| **Month** | Where the money went, against what you meant it to |
| **Transactions** | The ledger, and the three things about a row you can change |
| **Net worth** | What you own and owe, and how that has moved |
| **Linked** | Refresh, review, commit |
| **Settings** | Categories, and what each is meant to cost |

### Rebuilt on 2026-09-18, and why

The version before this had nineteen tables and eight screens, and most of the
difference was not features — it was structure inherited from the Excel
workbook it replaced. Measured against three years of real data, the parts that
went had nothing behind them:

- **No fixed cost had ever been superseded.** All twenty-nine rows carried
  `effective_from`/`effective_to` so that changing a commitment's amount could
  close one row and open another. In three years that path never ran once.
- **Thirteen of twenty-nine were parent/child components**, which is the only
  reason every fixed-cost total had to remember `parent_id.is_(None)`.
- **`is_exact` was true on twenty-seven of twenty-nine.** A flag with no
  variance is a comment.
- **Budgets went unmaintained.** Twelve allocations for August 2026; exactly
  one each for May, June and July.
- **Reconciliation had finished its job.** It existed to prove the workbook
  import landed correctly, and it did.

What survived is what the banks feed and what reads it. The 1,291 hand-typed
workbook rows covering December 2023 to August 2026 were not ported — the app
now holds what the institutions report and nothing else.

### Nothing is stored that can be derived

Spending by category, net worth over time, budget versus actual: all queries,
none of them tables. The workbook kept its rollups as cells computed by SUMIF
over a hardcoded list of category names, and any category missing from that
list silently vanished from the total. Two real cases: May 2024 under-reported
spending by $28.94 because a charge was typed `' Subscription'`, and monthly
fixed costs under-reported by $89.00 because the gym was filed under a category
the rollup list omitted — making disposable income optimistic by exactly that.

A total that is a query cannot drift from the rows it totals.

### A card payment is not spending

`CategoryKind` is `SPENDING`, `INCOME`, `TRANSFER` or `SAVINGS`, and it exists
because of a bug that took three commits to stamp out. A credit card payment is
last month's spending moving between two accounts you own. Counting it reported
August 2026 as $2,278.84 when $207.45 of that was the Discover payment — and
made that payment the second largest "purchase" of the month, ranked above the
power bill, describing nothing.

Every figure claiming you spent something now runs through one `spending_only`
helper in `queries.py`. Written out by hand at each call site, it came out
correct in three places and missing in two.

The period list still counts every row and totals only the spending ones —
summed conditionally, never filtered, so a month is listed at its true row
count even when part of it is transfers.

### Committed versus flexible is gone

Deliberately. It was a property of the individual transaction, which was right,
but maintaining it meant maintaining a list of standing commitments, and that
list is what most of the deleted schema was. If it comes back it comes back as
a flag on a row with a rule behind it, not as a table of effective-dated
amounts.

### Money is a string on the wire

Every amount crosses the API as a string and stays one in the browser.
JavaScript has no decimal type: a JSON number becomes a float the moment it is
parsed, and a budget three cents off has no explanation available to whoever is
reading it. Sums happen in Postgres or in Python with `Decimal`. Nothing in the
frontend adds money up.

The single exception is the budget bar's width, which is a percentage for CSS.
The exact figures sit beside it in their own columns, so a rounded width can
never be mistaken for an amount.

### Net worth over time

Balances are one row per account per day, so the series is a `group by`. The
workbook stored them wide — a new *column* per snapshot date — which is why two
years of it produced eight readings.

Liabilities are stored **negative**, decided once at the point of writing, so
net worth is a plain sum and no query has to remember the sign rule. Plaid
reports $207.45 owed as `+207.45`; the flip happens on the way in.

Every point carries how many accounts reported that day. Without it, a day when
two of eleven accounts were read looks exactly like a day net worth collapsed.
Partial days are held out of the line and counted underneath instead.

### Holdings are priced by the institution

This used to be 1,500 lines. A Fidelity CSV was parsed for positions, public
quotes were fetched to revalue them, and for the largest holding in the
portfolio — `31564E540`, a collective investment trust sold only inside
employer plans, with no ticker and no public quote that will ever exist — the
code borrowed a similar fund's daily movement and scaled the last statement
value by it. Four separate pricing bases had to be displayed side by side,
because they were four different kinds of claim.

Fidelity can be linked through Plaid as of 2026-09-18, so the institution
reports its own prices and none of that is needed. `cost_basis` stays nullable
and a null stays a null — zero would report an entire position as gain.

### The merchant is chosen at entry, not merged afterwards

A merchant is a string on the transaction. There is no merchant table, no merge
queue and no similarity scoring, and the reason is structural: resolving a
merchant from the descriptor *on write* means a second spelling can only be
discovered once it is already in the data, so something has to fold duplicates
back afterwards. Choosing from the names already in use means the second
spelling never arrives.

`snap_to_existing` has exactly two rules, both exact:

1. Same name, different case — `harris teeter` joins `Harris Teeter`.
2. An existing name that is a whole-word **prefix** of the guess, longest
   wins — so `UBER EATS 4471` resolves to `Uber Eats` and not to `Uber`.

Rule 2 exists because the original schema kept a pattern per descriptor, and a
hand-merge had taught it that `NETFLIX.COM` was Netflix. Dropping the patterns
lost that, and the guess regressed to `Netflix Com`.

Any future proposal to add fuzzy matching, a merge screen or a similarity score
is re-creating what was deliberately removed. The fix for a bad guess is the
picker on the row.

### The normalizer

`normalize_merchant` strips an appended id only when the run **contains a
digit**. Descriptors arrive in capitals, so matching on length alone ate
merchant names instead: `DUKEENERGY`, `POTBELLY`, `CHARLOTTE OBSERVER`. With
that rule missing, `DUKEENERGY BILL PAY 910175813041` collapsed to `bill pay` —
close enough to `BILT CARD HOUSING` that the power bill and the rent were
offered as the same merchant.

### Signing in

Password, or a passkey. A six-digit code is **off by default** and is one line
of config away (`REQUIRE_TOTP=true`).

That default is a judgement about this deployment, not about second factors.
The app is bound to loopback and has exactly one user; the attacker a TOTP code
defends against — someone holding the password but not the phone — has to
already be typing on the machine that holds the database, where they can read
it directly and skip the login entirely. What the code reliably did instead was
stand between the owner and their own budget, twice, and the end of that road
is a login that gets disabled altogether.

**A passkey is the better answer and is unaffected by this.** Touch ID is one
gesture, is multi-factor on its own (`user_verification=REQUIRED` is what makes
that true), and is less friction than typing a password. The Plaid security
questionnaire was answered on the basis that MFA exists, so if the code stays
off, a passkey should be registered.

The guard is a dependency on the **`/api` router itself**, not per-route, so
the default is closed — a router added later is protected by virtue of being
mounted, and every exception is written down in `auth.PUBLIC_PATHS` where it
can be read. The per-route alternative fails silently: a handler that forgets
the dependency is simply open, and nothing about it looks wrong.

Before any user exists the API is open, so first launch can reach setup.
`/auth/setup` refuses once a user is confirmed, which closes it for good.

A passkey is **MFA on its own** — `user_verification=REQUIRED` is what makes
that true — so signing in with one asks for nothing else. `webauthn_rp_id` is
configuration: a passkey is bound to it, so moving to a real hostname
invalidates every registered passkey, and password + TOTP is the fallback that
makes that survivable.

Locked out: `cd backend && uv run python scripts/reset_auth.py --apply`. Dry
run by default, deletes the user only, touches no financial data.

### Linked banks

Link an institution once through Plaid; every refresh pulls what has posted
since the last one.

**Preview first, commit second.** A bank feed guesses a merchant and a category
exactly as an import does, and a guess that writes itself is a guess nobody
checks.

#### The cursor is what protects the data

Plaid hands back changes since a cursor. **The cursor advances only on commit**,
never on a preview, so closing the screen without acting re-offers the same rows
next time rather than stepping past them for good. A row you *untick* is a
decision that it does not belong, and the cursor passes it too — `Re-offer
everything` clears the cursor when that was a mistake, and rows already
committed are filtered out by their transaction id, so only the genuinely
missing ones come back.

If a sync offers **zero** reviewable rows the cursor advances immediately.
Otherwise a rewind that re-offers nothing repeats forever.

#### A charge is not immutable, so a content hash is the wrong key

A CSV is a snapshot; a feed is a record the bank keeps editing. A pending coffee
posts two days later with a different date, often a different amount, and
usually a rewritten descriptor — and a hash over content sees a brand-new
transaction where there is one charge. A linked row is keyed by **Plaid's
transaction id**.

Revisions and withdrawals are applied **silently**: the bank settling its own
record is not a suggestion, and holding it behind a prompt leaves the ledger
knowingly wrong until someone clicks. A revision updates only the fields the
bank owns — date, amount, descriptor — so it cannot discard a category or
merchant corrected by hand.

**Pending charges are never offered.** They get withdrawn and re-sent when they
post; waiting costs a day and saves reviewing the same coffee twice.

#### Products are fixed when an item is created

An item keeps the product set it was created with. Adding `investments` to a
bank linked for transactions means running Link again in update mode with fresh
consent — so the Linked screen has two buttons, and a brokerage linked with the
wrong one returns balances and no holdings, which reads as an empty portfolio
rather than as a mislink.

Access tokens are **Fernet-encrypted** (`PLAID_TOKEN_KEY`). A Plaid access
token is a long-lived read key to a real bank account, which is a different
class of secret from anything else here, and this database gets dumped like
ordinary data. Changing that key orphans every linked bank.

**The tokens are the irreplaceable asset**, not the code. Production items are
finite per account, so recovering from a dropped table means re-linking every
bank and spending that many more. `scripts/extract_plaid_items.py` and
`scripts/restore_plaid_items.py` exist for exactly that, and were what made the
2026-09-18 rebuild safe.

### Seeding

```sh
cd backend && uv run python scripts/seed_categories.py
```

Idempotent. An existing name is left exactly as it is, so running it twice
cannot renumber or re-kind a category that has since been edited.

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
- **Sessions run with `autoflush=False`, and the test fixture must match.**
  `SessionLocal` sets it; a bare `Session()` defaults the other way. When the
  fixture disagreed, endpoint code that never flushed passed its tests and
  raised a unique violation in production — that is exactly how the import
  endpoint came to 500 on two identical rows while its tests were green. If a
  query has to see a row you just added, flush it yourself.
- **Keep this repo out of iCloud** (`~/Documents`, `~/Desktop`). iCloud sets the
  macOS `UF_HIDDEN` flag on files it manages, and CPython's `site.addpackage()`
  silently skips hidden `.pth` files. That kills the editable install and you get
  `ModuleNotFoundError: No module named 'backend'` with nothing in the traceback
  pointing at the cause. Diagnose with `ls -lO .venv/lib/python3.12/site-packages/*.pth`
  — a `hidden` flag in that column is the tell. It lives in `~/Coding` for this reason.
