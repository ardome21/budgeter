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

Six screens, all reading and writing the same database the workbook was
imported into, behind a login.

| Screen | What it replaces |
|---|---|
| **Month** | The `*Budget` sheets — allocations vs actuals, committed/flexible, pace |
| **Transactions** | The `*Spending` sheets — list, add by hand, recategorise inline |
| **Linked** | Downloading an export at all — refresh straight from the bank |
| **Import** | Pasting a bank export into a sheet, then categorising it by hand |
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

### The merchant is chosen at entry, not merged afterwards

A transaction records **who was paid as a name on the row** (`merchant_key`),
not a foreign key into a merchant table. That is a deliberate reversal.

The tables it replaces resolved a merchant from the description on write, which
meant a second spelling could only be discovered *after* it was already in the
data — so there was a review queue to fold the duplicates back together. That
produced 403 merchants from 1,291 transactions, **278 of them used exactly
once**, and a queue nobody could empty. Its suggestion pass was also O(n²):
88 ms at 403 names, 57 s at 10,000.

Offering the names already in use **at the moment of entry** means the second
spelling never arrives. The picker (`GET /merchants/keys`) appears on the entry
form, on every transaction row, and on every import preview row, ranked by use
rather than alphabetically because most names are used once.

Free typing is still allowed — a shop nobody has visited has to be nameable —
and what is typed is snapped to an existing spelling by two **exact** rules,
never a similarity score:

1. Same name, different case: `harris teeter` is `Harris Teeter`.
2. An existing name that is a whole-word **prefix** of the guess, longest
   first. `UBER EATS 4471` is `Uber Eats`, not `Uber`.

Rule 2 also covers several descriptor forms the normalizer alone misses, since
it never sees a real bank export until one is imported: `SPOTIFY USA 8774471`,
`HARRIS TEETER 0412` (no `#`, and four digits is under the 8-character id
floor) and `LYFT *RIDE THU 3PM`, which otherwise minted a merchant per ride.

**What this gives up**, honestly: there is no longer any way to merge two names
after the fact. The old schema kept a pattern per descriptor, so hand-merges
taught it that `Netlix` and `Harris Teater` were typos of real shops. Those
corrections are gone, and a typo now has to be fixed in the picker on the row.
That is the trade — and it is a good one here, because every one of those typos
came from a hand-typed workbook cell that no bank will ever send again.

### The normalizer

`normalize_merchant` collapses a descriptor to a stable key, and it is only ever
a **guess** that pre-fills an editable field. It strips processor prefixes
(`TST*`, `SQ *`), phone numbers, store numbers, trailing state codes, known city
names and opaque bank references.

It strips an appended id by looking for **a long run containing a digit**,
because that is what separates an identifier from a word. Matching on length
alone ate the merchant instead: descriptors arrive in capitals, so
`DUKEENERGY BILL PAY 910175813041` became `bill pay`, one typo away from
`BILT CARD HOUSING` — and the power bill and the rent were duly treated as one
merchant.

### Money is a string on the wire

Every money field crosses the API as a JSON **string**, never a number.
JavaScript has no decimal type, so a JSON number is a float the moment it is
parsed. The browser formats what it is handed and never sums anything — every
total on screen was computed in Postgres.

### Signing in

The app holds three years of financial history and, since linking, long-lived
read tokens to real bank accounts. It had no authentication at all before that,
which was defensible on loopback and is not once those tokens exist.

**First run claims the app.** Visit it and you get a setup screen: a name, a
password, then a QR code for any authenticator app and ten recovery codes.
Enrolment is two steps on purpose — the account is not usable until a code
generated from that secret comes back, because a mistyped scan would otherwise
lock the only person there will ever be out of their own history.

Setup is reachable only while no confirmed user exists, and refuses afterwards.
That is the whole window in which the API is open.

**Both factors fail together.** A wrong password and a wrong code return the
same message, because saying which one was right tells an attacker holding one
of them that it works. Eight failures lock the account for fifteen minutes.

**What is stored, and how.** The password is Argon2id. The TOTP secret is
Fernet-encrypted with the same key as the Plaid tokens — a secret in the clear
would make the second factor decorative against exactly the attacker it exists
to stop, one who has read the database. Recovery codes are hashed like
passwords, because a recovery code *is* a password, and each works once.

**The guard hangs off the `/api` router**, not off each route, so the default is
closed: a router added later is protected by virtue of being mounted, and every
exception is written down in `PUBLIC_PATHS` where it can be read. The per-route
alternative fails silently — a handler that forgets the dependency is simply
open, and nothing about it looks wrong. Session expiry is checked server-side as
well as on the cookie, since a cookie's own max-age is a hint an attacker
replaying a stolen one is free to ignore.

#### Passkeys: Touch ID instead of a code

Once you are in, **Settings → Signing in** registers this device. After that,
signing in is one gesture and the six-digit code becomes the thing you never
use.

A passkey is multi-factor **on its own**: the private key never leaves the
machine (what you have) and the authenticator will not use it without a
fingerprint or the device password (what you are, or know). That is why signing
in with one asks for nothing else — a password prompt in front of it would be
theatre, not a third factor. `userVerification` is `required`, which is what
makes that true rather than assumed.

Registering is behind the login; signing in is not. Adding a passkey sets a
credential, so it has to sit behind the login it will go on to replace.

The relying-party id is `localhost`, and a passkey is bound to it. Serving the
app from a real hostname invalidates every passkey already registered, which is
why `WEBAUTHN_RP_ID` is configuration rather than a constant — the password and
code keep working, and the passkeys get registered again.

Nothing secret is stored. The public key verifies signatures and cannot produce
them, so unlike the TOTP secret that table is not worth encrypting. The one
attack the protocol can detect is a cloned authenticator — a signature counter
that has not advanced past what was last seen — and that is refused.

**Why not email or SMS.** SMS needs a paid provider and is the weakest factor
there is; email needs SMTP credentials and is usually the reset path for
everything else, which makes it close to circular. Both add a delivery failure
mode: no signal, no internet, or a provider having a bad day, and you cannot
open your own budget on your own laptop. For an app that runs on one machine,
an external service in the login path is a lot of machinery for a downgrade.
That calculus changes the day this has more than one user or needs recovery on
a device you do not have.

#### No authenticator yet

There is a gap between setting the login up and having an authenticator that
actually works, and being locked out of your own budget in the middle of it is
the worst moment for it to happen:

```sh
cd backend && uv run python scripts/show_code.py             # the current code
cd backend && uv run python scripts/show_code.py --setup-key # and the secret
```

Honest about what it costs: anyone who can run it already holds the database
and the encryption key, so it hands them the second factor too. It is no weaker
than `reset_auth.py` beside it, which can delete the login outright — both need
shell access to the machine, which is a higher bar than the login asks for.

**The point is to stop needing it.** Register a passkey, or paste the setup key
into an authenticator, and never run it again.

#### Locked out

Recovery codes first. If those are gone too:

```sh
cd backend && uv run python scripts/reset_auth.py          # says what it would do
cd backend && uv run python scripts/reset_auth.py --apply  # does it
```

It clears the login and nothing else — every transaction, account and linked
bank stays exactly where it is, and the next visit runs setup again. It needs
shell access to the machine holding the database, which is a higher bar than
the login itself asks for, and that is what makes it safe to have.

### Linked banks

A **Refresh** button instead of a trip to five bank websites. Link an
institution once through Plaid, and every refresh pulls what has posted since
the last one.

The same rule as the CSV import governs it: **preview first, commit second**. A
bank feed guesses a merchant and a category exactly as a CSV does, and a guess
that writes itself is a guess nobody checks.

Set up with `PLAID_CLIENT_ID`, `PLAID_SECRET` and `PLAID_TOKEN_KEY` in `.env` —
see `.env.example`. Without them the Linked screen says so and every other
screen carries on unchanged.

#### The cursor is what protects the data

Plaid hands back changes since a cursor. The cursor advances **only on commit**,
never on a preview, so closing the screen without acting re-offers the same
rows next time rather than stepping past them for good. A row you *untick* is a
decision that it does not belong, and the cursor passes it too — `Re-offer
everything` clears the cursor when that was a mistake, and rows already
committed are filtered out by their transaction id, so only the genuinely
missing ones come back.

#### A charge is not immutable, so a content hash is the wrong key

This is where a bank feed differs from a CSV. A CSV is a snapshot; a feed is a
record the bank keeps editing. A pending coffee posts two days later with a
different date, often a different amount, and usually a rewritten descriptor —
and a hash over content sees a brand-new transaction where there is one charge.

So a linked row is keyed by **Plaid's transaction id**, in `source_ref`. That
column already existed for workbook provenance (the cell a figure came from),
which is why its unique index is partial: the column is a key on one side of
`source` and a note on the other.

Revisions and withdrawals are applied **silently**. They are the bank settling
its own record, not a suggestion, and holding them behind a prompt would leave
the ledger knowingly wrong until someone clicked. The screen reports the counts.

**Pending charges are not offered at all.** They get withdrawn and re-sent when
they post; waiting costs a day and saves reviewing the same coffee twice.

#### Two things it does not solve

**Fidelity cannot be linked.** It shares data only through Akoya, which has no
individual developer access. Fidelity stays on balance snapshots and the CSV
screen — which is the reason that screen stays.

**Bilt moved.** The Wells Fargo card was retired in February 2026 and Bilt Card
2.0 is issued by Cardless, so the historical `BILT CARD HOUSING` rows and
anything linked today sit on either side of that change.

#### What it does not touch

Linked accounts are created fresh. The lumped `Credit Cards` account and the
1,291 workbook rows filed against it are left exactly as they are — the split
is simply the day an institution was linked. Each linked institution also gets
a **start date**, defaulting to the day after the newest transaction on file:
Plaid returns up to 24 months, the workbook already holds all of them, and
without the floor a first refresh is a thousand rows of near-duplicates against
history typed by hand.

Access tokens are Fernet-encrypted before they are stored. A Plaid access token
is a long-lived read key to a real bank account, which is a different class of
secret from anything else here, and this database gets dumped like ordinary
data.

### Importing a bank export

Still here, and still the answer for anything no aggregator reaches.

Preview first, commit second; nothing is written until the preview comes back
and you confirm it. Columns are detected across the usual header spellings,
amounts parse from `$1,234.56` / `(45.00)` / `-45`, and there is a toggle for
banks that export purchases as negative.

The merchant is guessed against the three years of history already imported, so
most rows arrive with the right category already filled in — and the guess sits
in an editable picker, because a wrong one has to be fixable here. Nothing
downstream will fix it.

A merchant does not *have* a category, it has a **history** of categories.
Rhino Market & Deli is Food and Drinks on a sandwich run and Groceries on a
shop, and both are right. So the preview suggests the most-used one and offers
every other category that merchant has genuinely been filed under as a
one-click chip, each showing how often.

There is no stored default to drift from that history, and nowhere left to put
one. The column that held it recorded whichever transaction created the
merchant, never followed a merge, and ended up contradicting the merchant's own
history for twelve of them.

#### Which account it came from

Name the account the export belongs to and every row it creates carries it.
It also goes into the row's hash, so the same charge appearing on a card export
and a checking export stays two rows instead of one silently swallowing the
other. The selector lists open accounts only — a settled account cannot take
new charges, and asking to import to one is refused rather than accepted and
filed somewhere odd.

#### Two ways a row can already be on file

Re-dropping the same file is a no-op: CSV rows carry a content hash, and rows
already present come back ticked off as duplicates. **Hand-entered rows are
never hashed and never deduplicated** — two identical charges at the same bar
on one night are usually two real rounds.

That last point is also true *inside* one export, and a hash over content alone
cannot express it. Two $3.50 coffees on one day hashed identically, and the
second one collided with the first against a unique index — the response was a
500 and the entire import was lost, not just the row. Repeats are now numbered
in the order they appear, so both survive, and a re-drop of the same file still
skips both because the same file numbers them the same way again.

The hash only ever catches an exact re-drop. It cannot catch the case that
actually bites: a bank export covering months the workbook already holds. Those
descriptions were typed by hand — `Netflix`, `Breakfast`, `Alex Dinner` — and
never hash the same as `NETFLIX.COM 866-579-7172 CA`. So the preview also flags
**near duplicates**: anything already on file for the same amount within three
days, shown with the row it matched.

Flagged rows stay ticked. A near match is a question, not a verdict, and
genuine repeat purchases look exactly like this — quietly discarding real
spending is the worse failure. When an export really does overlap a period you
already have, one button unticks all of them at once.

The imported workbook history was hashed after the fact by
`scripts/backfill_import_hashes.py`, so exact matches against it work too. It
is a dry run by default, like every script under `scripts/`.

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
