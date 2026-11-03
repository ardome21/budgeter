# How budgeter works

What the app includes as of 2026-11-02, and the rules each figure on screen
rests on. Every diagram here is drawn from the code; the module that owns a
rule is named beside it, so a diagram that drifts can be checked.

1. [What's in the app](#1-whats-in-the-app)
2. [The data](#2-the-data)
3. [Refresh: getting the banks' data in](#3-refresh-getting-the-banks-data-in)
4. [Filing a transaction](#4-filing-a-transaction)
5. [The month](#5-the-month)
6. [Budgets](#6-budgets)
7. [Net worth over time](#7-net-worth-over-time)
8. [Where you stand: the Overview](#8-where-you-stand-the-overview)
9. [Goals](#9-goals)
10. [Signing in](#10-signing-in)

---

## 1. What's in the app

Seven screens, one API under `/api`, one Postgres database. Plaid is the only
outside service.

```mermaid
flowchart LR
  subgraph Screens["Angular screens"]
    OV[Overview]
    GO[Goals]
    MO[Month]
    BU[Budget]
    TX[Transactions]
    NW[Net worth]
    LI[Linked]
  end

  subgraph API["FastAPI · /api · login required by default"]
    A_OV["/overview · /pay"]
    A_GO["/goals · /goals-order"]
    A_MO["/months · /month/y/m · /regular"]
    A_BU["/categories · /budgets/y/m · /merchant-rules"]
    A_TX["/transactions · /merchants · /refile-uncategorised"]
    A_NW["/net-worth · /portfolio · /accounts"]
    A_LI["/linked/items · /link-token · /exchange · /refresh"]
    A_AU["/auth/* · /passkeys/*"]
  end

  subgraph Logic["Domain modules"]
    overview.py
    goals.py
    queries.py
    categorize.py
    merchants.py
    rebuild.py
    plaid_client.py
    auth.py
  end

  OV --> A_OV --> overview.py
  GO --> A_GO --> goals.py
  MO --> A_MO --> queries.py
  BU --> A_BU --> queries.py
  TX --> A_TX --> categorize.py
  NW --> A_NW --> queries.py
  LI --> A_LI --> plaid_client.py
  A_LI --> rebuild.py
  A_AU --> auth.py
  goals.py --> overview.py --> queries.py

  plaid_client.py <--> Plaid[(Plaid)]
  Logic --> DB[(PostgreSQL)]
```

| Screen | The question it answers |
|---|---|
| **Overview** | Where do I stand today, by kind of money, and how much of what I earn do I keep? |
| **Goals** | What am I saving for, how much of it is already covered, and when will each goal be reached? |
| **Month** | Where did the money go, against the budget, against last month, and what regular charges are still to come? |
| **Budget** | What should each category cost? Set once, carried forward. Also where categories are managed. |
| **Transactions** | The ledger, with category, merchant and notes editable per row. |
| **Net worth** | What I own and owe and how that has moved, plus holdings. |
| **Linked** | Every bank and account, and whether each one is still reporting. |

### Rules that hold everywhere

- **Nothing that can be derived is stored.** Spending, net worth, budget vs actual, goals and regular charges are all computed when you ask.
- **Money is a decimal string on the wire.** Sums are done in Postgres or in Python `Decimal`. The browser adds money only in whole-cent `bigint` (`money.ts`).
- **Only `SPENDING` counts as spent.** A card payment is a `TRANSFER`, so it never counts as a purchase.
- **Liabilities are stored negative.** That way net worth is a plain sum.

---

## 2. The data

Seventeen tables. The banks fill `accounts`, `balances`, `transactions`,
`holdings`, `securities` and `contributions`. You fill `budgets`, `goals`,
`pay_stubs`, `merchant_rules` and the category map.

```mermaid
erDiagram
  PLAID_ITEMS ||--o{ ACCOUNTS : "reports"
  ACCOUNTS ||--o{ BALANCES : "one per day"
  ACCOUNTS ||--o{ TRANSACTIONS : ""
  ACCOUNTS ||--o{ HOLDINGS : ""
  ACCOUNTS ||--o{ CONTRIBUTIONS : ""
  SECURITIES ||--o{ HOLDINGS : ""
  CATEGORIES ||--o{ TRANSACTIONS : "filed under"
  CATEGORIES ||--o{ BUDGETS : ""
  CATEGORIES ||--o{ PLAID_CATEGORY_MAP : ""
  CATEGORIES ||--o{ MERCHANT_RULES : ""
  PAY_STUBS ||--o{ PAY_LINES : ""
  ACCOUNTS |o--o{ PAY_LINES : "goes into"
  APP_USERS ||--o{ PASSKEYS : ""
  APP_USERS ||--o{ RECOVERY_CODES : ""

  PLAID_ITEMS {
    string item_id
    text access_token "Fernet-encrypted"
    text cursor "advances in the same commit as its rows"
    string products "fixed at link time"
    bool needs_reauth
  }
  ACCOUNTS {
    string plaid_account_id
    string account_type
    string subtype
    bool is_liability
    bool is_retirement
    date closed_on
  }
  BALANCES {
    date as_of
    decimal amount "negative for debt"
    string source "bank | archive | rebuilt"
  }
  TRANSACTIONS {
    string plaid_transaction_id "the key, not a content hash"
    date occurred_on
    decimal amount
    string description
    string merchant "a plain string"
    bool pending
    string plaid_category_primary
    string plaid_category_detailed
  }
  CATEGORIES {
    string name
    enum kind "SPENDING | INCOME | TRANSFER | SAVINGS"
  }
  BUDGETS {
    date month
    decimal amount
  }
  PAY_STUBS {
    date effective_from
    int periods_per_year
    decimal gross
  }
  PAY_LINES {
    enum kind "TAX | SAVING | DEDUCTION | EMPLOYER"
    decimal amount
  }
  GOALS {
    decimal amount "or months_of_spending"
    date target_date "optional"
    int sort_order "the priority"
  }
```

An account's **group** is derived, not stored (`Account.group`). The order
matters: retirement is checked before investment.

```mermaid
flowchart LR
  A{is_retirement?} -->|yes| R[retirement]
  A -->|no| B{type investment?}
  B -->|yes| I[investment]
  B -->|no| C{type credit?}
  C -->|yes| CR[credit]
  C -->|no| D{is_liability?}
  D -->|yes| L[loan]
  D -->|no| E{depository?}
  E -->|yes| CS[checking / savings]
  E -->|no| O[other]
```

---

## 3. Refresh: getting the banks' data in

There is no review step: what the banks send is written straight to the
database. A refresh runs on its own the first time the app is opened each
day (`sync.ts`), from the header's **Refresh** button, or from
`scripts/refresh.py`.

```mermaid
sequenceDiagram
  autonumber
  participant UI as Browser (sync.ts)
  participant API as POST /api/linked/refresh
  participant P as Plaid
  participant DB as Postgres

  UI->>API: first open of the day, or Refresh
  loop each PlaidItem, by institution
    API->>API: decrypt access token
    API->>P: accounts + balances
    P-->>API: accounts, today's balances
    API->>DB: upsert accounts, one balance per (account, day)
    alt login stale
      API->>DB: needs_reauth = true, commit
      Note over API: named in failures, next bank
    end
    opt item has investments
      API->>P: holdings, investment transactions
      API->>DB: holdings, contributions
    end
    opt item has transactions
      API->>P: /transactions/sync from cursor
      P-->>API: added, modified, removed, next cursor
      API->>DB: adopt posted · removals · revisions · new rows
      API->>DB: cursor = next, only if no row lacked an account
    end
    API->>DB: commit this bank
    Note over API,DB: any error rolls back this bank only
  end
  API->>DB: delete rebuilt balances, recompute them
  API-->>UI: counts + failures[]
  UI->>UI: bump version, open screens reload
```

The order inside one bank's transaction sync is what keeps a charge from being
counted twice or lost:

```mermaid
flowchart TD
  S[sync diff from cursor] --> A["1 · adopt posted<br/>a posted row turns its pending row into itself, in place"]
  A --> R["2 · removals<br/>rows the bank withdrew are deleted"]
  R --> V["3 · revisions<br/>only bank-owned fields change: date, amount, descriptor, labels<br/>your category, merchant and notes are kept"]
  V --> N["4 · new rows<br/>skipped if the id is known or before sync_start_on<br/>merchant resolved, category resolved, pending kept"]
  N --> H{any row whose account<br/>is not here yet?}
  H -->|no| C[advance cursor]
  H -->|yes| K["hold cursor<br/>re-asked next time; known ids are skipped"]
```

---

## 4. Filing a transaction

Every write path calls the same two functions, `merchants.resolve` and
`categorize.resolve_category`.

### Merchant

A merchant is a plain string, and it snaps to names already in use, so a
second spelling never gets in.

```mermaid
flowchart TD
  IN[descriptor + Plaid merchant_name] --> G{Plaid gave a name?}
  G -->|yes| GUESS[use it]
  G -->|no| NORM["normalize_merchant<br/>strip a trailing id only if it contains a digit"]
  NORM --> GUESS
  GUESS --> S1{same name, other case,<br/>already in use?}
  S1 -->|yes| USE1[use the existing spelling]
  S1 -->|no| S2{an existing name is a<br/>whole-word prefix?}
  S2 -->|yes| USE2["use the longest such name<br/>UBER EATS 4471 → Uber Eats"]
  S2 -->|no| NEW[new merchant]
```

### Category

The first rule that answers wins. A row you change by hand is never
overwritten by a re-sync.

```mermaid
flowchart TD
  T[incoming row] --> D{"descriptor starts with a<br/>TRANSFERS_BY_DESCRIPTION entry?<br/>e.g. BILT CARD HOUSING"}
  D -->|yes| TR[Transfer]
  D -->|no| M{"a merchant rule<br/>for this merchant?"}
  M -->|yes| MR[the rule's category]
  M -->|no| PD{"Plaid detailed label<br/>in plaid_category_map?"}
  PD -->|yes| PDC[mapped category]
  PD -->|no| PP{"Plaid primary label<br/>in the map?"}
  PP -->|yes| PPC[mapped category]
  PP -->|no| U["Uncategorised<br/>created on first use"]
```

The descriptor check comes before merchant rules on purpose, so that no rule
can turn a transfer back into spending. Merchant rules are set from the list
of regular charges. **File everything uncategorised** re-runs this chain over
existing rows using the Plaid labels stored on them.

**Rent through Bilt** arrives as three rows and is counted once:

| Row | Plaid says | Filed as | By |
|---|---|---|---|
| `BILT CARD HOUSING` debit, Chase | rent | Transfer | descriptor rule |
| `Payment - Bilt Housing` credit, Bilt | rental income | Transfer | map: `INCOME_RENTAL` |
| `Bilt Housing Payment` charge, Bilt | rent | Rent | map |

---

## 5. The month

`queries.month_summary` puts every row in the month into a bucket by its
category's kind in a single pass. Everything else on the Month screen reads
from those buckets.

```mermaid
flowchart TD
  ROWS[transactions in the month] --> K{category kind}
  K -->|SPENDING| SP[spent]
  K -->|INCOME| IN[money in]
  K -->|TRANSFER| X[ignored by totals]
  K -->|SAVINGS| SV[saved, shown apart]

  BS["budget_source<br/>latest month with a budget, on or before this one"] --> B[budgeted]
  B --> ETD["expected to date =<br/>budget × days elapsed ÷ days in month"]

  SP --> ST{spent vs budget}
  B --> ST
  ETD --> ST
  ST -->|spent > budget| OB[over budget]
  ST -->|spent > expected to date| AP[ahead of pace]
  ST -->|otherwise| OK[on track]

  IN --> LE["left_expected =<br/>max(money in, income budget) − spent"]
  SP --> LE
  LE --> LA["left_after_upcoming =<br/>left − pending − still to come"]
  UP[still to come] --> LA
  B --> LP["left_planned =<br/>income budget − spending budget"]
```

The states are computed in `Decimal` on the server and sent to the browser
as booleans.

### Regular charges and "still to come"

These are worked out from the rows each time, never stored.

```mermaid
flowchart LR
  H[last three months] --> Q{"same merchant charged in each month<br/>· amount within 20%<br/>· day within a week<br/>· no more than twice a month"}
  Q -->|yes| REG[regular charge]
  Q -->|no| NOT["not predicted<br/>e.g. a swinging electric bill"]
  REG --> E{already charged<br/>this month?}
  E -->|no| UP["still to come<br/>at last month's amount, on last month's day"]
  E -->|yes| DONE[done]
```

---

## 6. Budgets

A month without a budget of its own uses the latest set before it, so a
budget is set once and only touched to change something.

```mermaid
flowchart TD
  OPEN[Budget page for month M] --> HAS{M has its own set?}
  HAS -->|yes| SHOW[show it]
  HAS -->|no| PROP[propose]
  PROP --> SPC["spending categories<br/>last month, rounded UP to the next $10"]
  PROP --> TYP["typical = median of the last 3 complete months<br/>missing months count as $0"]
  PROP --> INC["income<br/>rounded DOWN"]
  SPC --> BOX[boxes filled, nothing saved]
  TYP --> BOX
  INC --> BOX
  BOX --> SAVE["Save → PUT /budgets/y/m<br/>replaces M's set whole"]
  SAVE --> CARRY["later months without a set<br/>carry this one forward"]
```

The rounding is deliberate. Rounding spending up means the proposal isn't a
budget you already broke last month. Rounding income down means the plan
doesn't count on money that might not arrive. The typical month uses the
median so one big holiday doesn't read as a habit.

---

## 7. Net worth over time

There is one balance row per account per day, and the series is a `GROUP BY`
over them. Plaid only reports today's balance, so the days in between are
rebuilt after every refresh.

```mermaid
flowchart TD
  R[refresh writes today's reading<br/>source = bank] --> RB[rebuild.rebuild_balances]
  ARC[source = archive<br/>Aug 2026 recovered readings] --> RB
  RB --> DEL[delete every rebuilt row]
  DEL --> T{account type}
  T -->|cash, cards, loans| TX["walk back from the next reading,<br/>putting the transactions in between back"]
  T -->|investment| LIN["straight line between real readings<br/>held flat back to the first reading of the rest"]
  TX --> W[write as source = rebuilt]
  LIN --> W
  W --> NWQ[queries.net_worth]
  NWQ --> P{all accounts reported<br/>that day?}
  P -->|yes| LINE[on the line]
  P -->|no| HELD[held out, counted underneath]
  LINE --> CH["chart: bands per group,<br/>hover a day, drag to compare two"]
```

A real reading is never overwritten by a rebuilt one. The chart says how many
of the days it draws are rebuilt, and marks a rebuilt day in its tooltip.

---

## 8. Where you stand: the Overview

`overview.py` has two halves. **Standing** is today's balances by kind of
money. **Flow** is each month: earned, spent and kept. The bank only sees
take-home pay, so the pay stub (entered once and carried forward) supplies
what happens before the money lands.

```mermaid
flowchart LR
  subgraph Standing["Standing, from latest balances"]
    CASH[cash: checking + savings]
    INV[investments you could sell]
    RET[retirement: can't touch yet]
    OWE[owed: cards + loans]
  end

  subgraph Stub["Pay stub, in force by effective_from"]
    GROSS["gross → stub take-home per check"]
    TAX[TAX]
    SAV["SAVING<br/>401k, HSA, ESPP"]
    DED[DEDUCTION]
    EMP["EMPLOYER<br/>match, on top of gross"]
  end

  subgraph Flow["Each month · stub lines × paychecks per month"]
    TH[take-home = money in, from the bank]
    KEPT[kept = take-home − spent]
    EARN["earned = take-home + TAX + SAVING + DEDUCTION<br/>worked back from what arrived, not the stub's gross"]
    SAVED["saved = kept + SAVING + EMPLOYER"]
    RATE["savings rate = saved ÷ (earned + EMPLOYER)"]
  end

  TH --> KEPT
  TH --> EARN
  TAX --> EARN
  SAV --> EARN
  DED --> EARN
  KEPT --> SAVED
  SAV --> SAVED
  EMP --> SAVED
  EARN --> RATE
  SAVED --> RATE
  EMP -. "plus the match measured from contributions,<br/>retirement plans only" .-> SAVED
```

Without a stub, the savings rate is kept ÷ take-home. In a month still
running, the stub is scaled to the paychecks that have actually landed, so on
the 2nd of the month a whole month's 401(k) isn't counted before any pay has
arrived. Typical figures are taken over the last three finished months. A month counts
as complete only if every bank has history for it, give or take a 7-day
grace period.

---

## 9. Goals

All goals draw on one pot: money you could actually spend on them. The pot is
split down the goal list in order, using cash first, and then each month's
reachable saving fills whatever is still needed.

```mermaid
flowchart TD
  subgraph Pot["The pot, from latest balances"]
    C["cash = checking + savings − cards − loans"]
    I["invested = investment accounts<br/>that are not retirement and not the HSA"]
  end

  subgraph Monthly["Reachable saving per month"]
    K[typical kept in the bank]
    E["stub SAVING lines into a reachable account<br/>e.g. ESPP → brokerage"]
  end

  G["goals in priority order<br/>target = amount, or months × typical spending"] --> SPLIT
  C --> SPLIT["split down the list:<br/>cash first, then invested"]
  I --> SPLIT
  SPLIT --> REM[remaining per goal]

  K --> M[monthly saving]
  E --> M
  REM --> PROJ["project month by month, up to 50 years"]
  M --> PROJ
  PROJ --> D{goal has a date?}
  D -->|yes| ASK["asks remaining ÷ months to go<br/>recomputed each month"]
  D -->|no| TAKE[takes whatever is left that month]
  ASK --> OUT["reached_on, on_track,<br/>monthly_needed, short_by"]
  TAKE --> OUT
```

No investment growth is assumed. A 401(k) counts as saving, but it can't be
spent on a house, so it isn't in the pot.

---

## 10. Signing in

There is one user. The login guard sits on the `/api` router itself, so any
route is protected unless it's listed in `auth.PUBLIC_PATHS`.

```mermaid
stateDiagram-v2
  [*] --> NoUser
  NoUser: No user (API open so setup can run)
  NoUser --> Setup: POST /auth/setup
  Setup: Password set, TOTP secret issued, recovery codes shown
  Setup --> SignedIn: POST /auth/setup/confirm (valid code)
  Setup --> Closed: setup refuses a second user from here on

  state Closed {
    [*] --> SignedOut
    SignedOut --> SignedIn: password (+ TOTP if REQUIRE_TOTP)
    SignedOut --> SignedIn: passkey (user verification = MFA)
    SignedOut --> SignedIn: password + recovery code
    SignedOut --> Locked: too many failed logins
    Locked --> SignedOut: lock expires
    SignedIn --> SignedOut: logout, or 14 days pass
  }
  SignedIn: Signed, HTTP-only session cookie, SameSite=lax
```

| Secret | Protects | If it changes |
|---|---|---|
| `SESSION_SECRET` | the session cookie | everyone is signed out, nothing else |
| `PLAID_TOKEN_KEY` | Plaid access tokens at rest | **every linked bank is orphaned** |
| `webauthn_rp_id` (config) | passkeys are bound to it | every passkey stops working |
| DB password | the database | update the secret and the DB together |
