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
  balance: string | null;
  balance_as_of: string | null;
}

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
}

export interface TransactionPage {
  rows: Transaction[];
  total: number;
}

export interface Budget {
  category_id: number;
  amount: string;
}

export interface CategorySpend {
  category_id: number;
  category_name: string;
  kind: CategoryKind;
  spent: string;
  budget: string | null;
  /** Null means no target set — a different claim from "nothing left". */
  remaining: string | null;
}

export interface MonthSummary {
  month: string;
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

export interface PreviewRow {
  transaction_id: string;
  occurred_on: string;
  description: string;
  merchant: string | null;
  amount: string;
  account_id: number;
  account_name: string;
  category_id: number | null;
  category_name: string | null;
  plaid_category: string | null;
}

export interface Preview {
  rows: PreviewRow[];
  modified: number;
  removed: number;
  accounts_updated: number;
  holdings_updated: number;
  cursor_advanced: boolean;
  message: string | null;
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
