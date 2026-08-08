/**
 * Types mirroring the API.
 *
 * Every money field is a `string`, and that is deliberate — JavaScript has no
 * decimal type, so parsing money as a number makes it a float and arithmetic on
 * it drifts by cents. The UI formats these for display and never sums them;
 * every total on screen was computed in Postgres.
 */

export type Money = string;

export type CategoryKind = 'SPENDING' | 'SAVINGS' | 'OTHER';
export type TransactionSource = 'WORKBOOK' | 'CSV' | 'MANUAL';

export interface Category {
  id: number;
  name: string;
  kind: CategoryKind;
  sort_order: number;
}

export interface Period {
  year: number;
  month: number;
  transaction_count: number;
  total: Money;
}

export interface CategoryLine {
  category_id: number;
  category: string;
  kind: CategoryKind;
  allocated: Money;
  spent: Money;
  remaining: Money;
  pct_used: number | null;
  share_of_spend: number;
}

export interface MonthSummary {
  year: number;
  month: number;
  categories: CategoryLine[];
  allocated_total: Money;
  spent_total: Money;
  remaining_total: Money;
  commitment: { committed: Money; flexible: Money; saved: Money };
  biggest: { description: string; amount: Money; occurred_on: string | null }[];
  days: { in_month: number; elapsed: number; remaining: number };
  spent_per_day: Money | null;
  safe_per_day: Money | null;
  transaction_count: number;
  undated_count: number;
}

export interface Overview {
  gross_monthly: Money;
  post_tax: Money;
  take_home: Money;
  fixed_costs: Money;
  disposable: Money;
  auto_saved: Money;
  paychecks_per_month: number;
  fixed_by_category: { category: string; amount: Money; lines: string[] }[];
}

export interface Transaction {
  id: number;
  occurred_on: string | null;
  year: number;
  month: number;
  raw_description: string;
  merchant_key: string | null;
  category_id: number;
  category_name: string;
  amount: Money;
  is_recurring: boolean;
  /** Null on the imported workbook history, which never recorded one. */
  account_id: number | null;
  account_name: string | null;
  source: TransactionSource;
}

export interface NewTransaction {
  occurred_on?: string | null;
  year?: number;
  month?: number;
  raw_description: string;
  /** Omit to have one guessed from the description; '' means no merchant. */
  merchant_key?: string | null;
  category_id: number;
  amount: string;
  is_recurring?: boolean;
  account_id?: number | null;
}

/** A category this merchant has actually been used with, and how often. */
export interface CategoryOption {
  id: number;
  name: string;
  count: number;
}

export interface PreviewRow {
  row_number: number;
  occurred_on: string | null;
  raw_description: string;
  amount: Money;
  suggested_category_id: number | null;
  suggested_category_name: string | null;
  /**
   * Every category this merchant has been filed under, most used first. A shop
   * is not one category — Rhino is Food and Drinks on a sandwich run and
   * Groceries on a shop, and both are right.
   */
  category_options: CategoryOption[];
  /** The guess. Editable on the preview — nothing downstream fixes it. */
  merchant_key: string | null;
  import_hash: string;
  duplicate_of: number | null;
  /**
   * Already on file for the same amount within a few days. A prompt to look,
   * not a duplicate — the hash cannot catch the workbook history, whose
   * descriptions were typed by hand and never match a bank descriptor.
   */
  near_duplicates: NearDuplicate[];
  notes: string[];
}

export interface NearDuplicate {
  id: number;
  occurred_on: string | null;
  raw_description: string;
  amount: Money;
  days_apart: number;
}

export interface Preview {
  rows: PreviewRow[];
  errors: string[];
  detected_columns: Record<string, string>;
  account_id: number | null;
  new_count: number;
  duplicate_count: number;
  near_duplicate_count: number;
  uncategorised_count: number;
}

export interface CommitResult {
  created: number;
  skipped_duplicates: number;
  errors: string[];
}

/** A merchant name in use, and how many rows carry it. Feeds the picker. */
export interface MerchantKey {
  key: string;
  count: number;
}

export const MONTH_NAMES = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
];

/** Format a money string for display. Never used for arithmetic. */
export function formatMoney(value: Money | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const negative = value.startsWith('-');
  const digits = negative ? value.slice(1) : value;
  const [whole, cents = '00'] = digits.split('.');
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return `${negative ? '−' : ''}$${grouped}.${cents}`;
}

/** An account with its latest balance and the move since the previous one. */
export interface AccountRow {
  id: number;
  institution: string;
  name: string;
  is_retirement: boolean;
  closed_on: string | null;
  latest_balance: Money | null;
  latest_as_of: string | null;
  /** Did not report on the most recent snapshot date, so this balance is
   *  history rather than a current position. */
  is_stale: boolean;
  days_behind: number | null;
  change: Money | null;
  snapshot_count: number;
}

export interface NetWorthPoint {
  as_of: string;
  net_worth: Money;
  retirement: Money;
  liquid: Money;
  /** How many accounts reported on this date — a total from five is not
   *  comparable to one from eight, and the chart should say so. */
  accounts_reported: number;
}

export interface NetWorth {
  points: NetWorthPoint[];
  accounts_tracked: number;
}

/**
 * Parse a YYYY-MM-DD calendar date as local time.
 *
 * `new Date('2024-09-04')` is parsed as UTC midnight and then rendered in the
 * viewer's zone, so anywhere west of Greenwich it displays as the 3rd. A
 * balance recorded on the 4th belongs to the 4th in every timezone.
 */
export function parseDay(iso: string): Date {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, (m ?? 1) - 1, d ?? 1);
}

export interface AllocationRow {
  category_id: number;
  category: string;
  amount: Money;
}

export interface Allocations {
  year: number;
  month: number;
  allocations: AllocationRow[];
  total: Money;
}

export interface FixedCost {
  id: number;
  description: string;
  amount: Money;
  category_id: number;
  category: string;
  is_exact: boolean;
  effective_from: string;
  effective_to: string | null;
  parent_id: number | null;
  merchant_key: string | null;
  /** The bill's own breakdown. Part of `amount`, never counted beside it. */
  components: FixedCost[];
}

export interface PaycheckLine {
  id: number;
  description: string;
  amount: Money;
  kind: 'INCOME' | 'INSURANCE' | 'SAVINGS' | 'TAX';
  effective_from: string;
  effective_to: string | null;
}

export interface ReconcileRow {
  fixed_cost_id: number;
  description: string;
  category: string;
  expected: Money;
  actual: Money | null;
  drift: Money | null;
  merchant: string | null;
  charges: number;
  note: string | null;
  /** Candidate merchant names for an unlinked commitment. */
  suggestions: string[];
}

export interface Reconciliation {
  year: number;
  month: number;
  rows: ReconcileRow[];
  expected_total: Money;
  matched_total: Money;
}

/** One account at a linked institution, as budgeter knows it. */
export interface LinkedAccount {
  account_id: number;
  name: string;
  mask: string | null;
  subtype: string | null;
}

/** A linked institution. Plaid calls it an Item; one bank, many accounts. */
export interface LinkedItem {
  id: number;
  institution_name: string;
  accounts: LinkedAccount[];
  sync_start_on: string;
  last_synced_at: string | null;
  /** The login has gone stale. Only re-running Link fixes it. */
  needs_reauth: boolean;
}

export interface PlaidStatus {
  /** False when there are no credentials in .env — say so rather than
   *  offering a button that can only fail. */
  configured: boolean;
  environment: string;
  items: LinkedItem[];
}

/**
 * One transaction the bank sent that is not yet on file.
 *
 * `key` is Plaid's transaction id and travels through to the commit unchanged.
 * It is identity, not a content hash: when a pending charge posts the date and
 * amount move, and this is what recognises it as the same charge.
 */
export interface SyncRow {
  key: string;
  account_id: number;
  account_label: string;
  occurred_on: string;
  raw_description: string;
  amount: Money;
  suggested_category_id: number | null;
  suggested_category_name: string | null;
  category_options: CategoryOption[];
  merchant_key: string | null;
  /** True when a standing commitment names this merchant. Committed-vs-flexible
   *  is a property of the transaction, so it is decided per row. */
  is_recurring: boolean;
  near_duplicates: NearDuplicate[];
  notes: string[];
}

export interface SyncResult {
  rows: SyncRow[];
  /** Charges the bank revised. Already applied — not a question. */
  updated: number;
  /** Charges the bank withdrew. Already removed. */
  removed: number;
  /** Account balances refreshed. A balance is a fact the bank states, not a
   *  guess to confirm, so it is reported rather than reviewed. */
  balances: number;
  near_duplicate_count: number;
  uncategorised_count: number;
  reauth_needed: string[];
  errors: string[];
}

export interface SyncCommitResult {
  created: number;
  skipped: number;
  errors: string[];
}

export interface AuthStatus {
  /** False before anyone has claimed the app — go to setup, not login. */
  configured: boolean;
  authenticated: boolean;
  username: string | null;
  /** Whether to offer the Touch ID button. Readable while signed out, because
   *  the login screen has to decide before anyone has proved anything. */
  has_passkeys: boolean;
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
