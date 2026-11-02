/**
 * The shapes the API sends.
 *
 * Every money field is a `string`, not a number, and stays one. JavaScript has
 * no decimal type: parsing "1234.56" into a float and adding it to another is
 * how a total ends up three cents off with no explanation. Nothing in the
 * browser sums money — the server does that — and these are rendered, not
 * computed with.
 */

export type CategoryKind = 'SPENDING' | 'INCOME' | 'TRANSFER' | 'SAVINGS';

export type SecurityKind =
  | 'EQUITY'
  | 'ETF'
  | 'MUTUAL_FUND'
  | 'FIXED_INCOME'
  | 'CASH'
  | 'OTHER';

export interface Category {
  id: number;
  name: string;
  kind: CategoryKind;
  sort_order: number;
}

export interface AccountRow {
  id: number;
  name: string;
  institution: string;
  mask: string | null;
  account_type: string | null;
  subtype: string | null;
  is_liability: boolean;
  is_retirement: boolean;
  closed_on: string | null;
  /** Derived on the server from what the bank reports. */
  group: AccountGroup;
  balance: string | null;
  balance_as_of: string | null;
}

export type AccountGroup =
  | 'checking'
  | 'savings'
  | 'credit'
  | 'loan'
  | 'retirement'
  | 'investment'
  | 'other';

export interface Transaction {
  id: number;
  occurred_on: string;
  description: string;
  merchant: string | null;
  /** Plaid's sign, unchanged: positive means money left the account. */
  amount: string;
  category_id: number;
  category_name: string;
  category_kind: CategoryKind;
  account_id: number;
  account_name: string;
  notes: string | null;
  /** How many other rows moved with this one, when the edit was applied to the
   *  whole merchant. Zero on an ordinary edit. */
  also_filed?: number;
}

export interface TransactionPage {
  rows: Transaction[];
  total: number;
}

export interface Budget {
  category_id: number;
  amount: string;
}

export interface BudgetSuggestion {
  category_id: number;
  category_name: string;
  /** The median of the last few complete months, not the mean. */
  amount: string;
  /** How many of those months this category appeared in. Seen once in three is
   *  a different proposal from seen every month. */
  months: number;
}

export interface CategorySpend {
  category_id: number;
  category_name: string;
  kind: CategoryKind;
  spent: string;
  budget: string | null;
  /** Null means no target set — a different claim from "nothing left". */
  remaining: string | null;
  /** What an evenly-spread budget allows by today. Null with no budget. */
  expected_to_date: string | null;
  /** Both decided on the server, in Decimal. Over budget is a fact about the
   *  month; over pace is a fact about today, and a category can be the second
   *  without being the first — which is the point of showing it mid-month. */
  over_budget: boolean;
  over_pace: boolean;
}

export interface MonthSummary {
  month: string;
  /** The denominator of every pace figure. */
  days_in_month: number;
  days_elapsed: number;
  /** What the spending budgets allow by today, or null when none are set —
   *  "on track" against no target is not a claim worth rendering. */
  expected_to_date: string | null;
  over_budget: boolean;
  over_pace: boolean;
  spent: string;
  income: string;
  saved: string;
  transferred: string;
  budgeted: string;
  categories: CategorySpend[];
  transaction_count: number;
}

export interface NetWorthPoint {
  as_of: string;
  assets: string;
  liabilities: string;
  net: string;
  /** How many accounts reported that day. A partial reading is not a crash. */
  accounts_reporting: number;
  /** Some figure on this day was rebuilt rather than read. */
  estimated: boolean;
  /** What the day is made of, summed by the server. */
  groups: DayGroup[];
}

export interface DayGroup {
  group: AccountGroup;
  total: string;
  accounts: DayAccount[];
}

export interface DayAccount {
  account_id: number;
  name: string;
  amount: string;
  /** bank and archive are readings; rebuilt is worked out. */
  source: 'bank' | 'archive' | 'rebuilt';
}

export interface NetWorth {
  points: NetWorthPoint[];
  latest: NetWorthPoint | null;
  account_count: number;
}

export interface Holding {
  security_id: number;
  name: string;
  ticker: string | null;
  kind: SecurityKind;
  is_cash_equivalent: boolean;
  account_id: number;
  account_name: string;
  quantity: string;
  price: string | null;
  value: string;
  cost_basis: string | null;
  /** Null when the institution did not report a basis. Never rendered as 0. */
  gain: string | null;
}

export interface Portfolio {
  as_of: string | null;
  total_value: string;
  total_cost_basis: string | null;
  /** How much of total_value the basis figure accounts for. Without it "paid
   *  in" reads as covering the whole portfolio. */
  cost_basis_covers: string;
  holdings: Holding[];
}

export interface LinkedItem {
  id: number;
  institution_name: string;
  products: string;
  needs_reauth: boolean;
  sync_start_on: string;
  last_synced_at: string | null;
  account_count: number;
}

/** What one Refresh did across every bank. */
export interface RefreshResult {
  added: number;
  modified: number;
  removed: number;
  accounts_updated: number;
  holdings_updated: number;
  /** Banks that could not be reached, named. */
  failures: string[];
}

export interface Refiled {
  moved: number;
  /** Rows nothing could place — what is left to do by hand. */
  left: number;
}

export interface AuthStatus {
  /** False before anyone has claimed the app — go to setup, not login. */
  configured: boolean;
  authenticated: boolean;
  username: string | null;
  /** Whether to offer the Touch ID button. Readable while signed out, because
   *  the login screen has to decide before anyone has proved anything. */
  has_passkeys: boolean;
  /** Whether a second factor is in force. Configuration, not a property of
   *  the user — so the code field is absent rather than optional. */
  totp_required: boolean;
}

export interface SetupResult {
  username: string;
  otpauth_uri: string;
  /** Rendered by the backend so the page needs no QR library and makes no
   *  external request — nothing third-party should see this URI. */
  qr_svg: string;
  /** Shown exactly once. No endpoint returns them again. */
  recovery_codes: string[];
}

/** A device registered to sign in with. Nothing secret — the public key
 *  verifies signatures and cannot produce them. */
export interface PasskeyRow {
  id: number;
  label: string;
  created_at: string;
  last_used_at: string | null;
}
