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
  merchant_id: number | null;
  merchant_name: string | null;
  category_id: number;
  category_name: string;
  amount: Money;
  is_recurring: boolean;
  source: TransactionSource;
}

export interface NewTransaction {
  occurred_on?: string | null;
  year?: number;
  month?: number;
  raw_description: string;
  category_id: number;
  amount: string;
  is_recurring?: boolean;
}

export interface PreviewRow {
  row_number: number;
  occurred_on: string | null;
  raw_description: string;
  amount: Money;
  suggested_category_id: number | null;
  suggested_category_name: string | null;
  merchant_name: string | null;
  import_hash: string;
  duplicate_of: number | null;
  notes: string[];
}

export interface Preview {
  rows: PreviewRow[];
  errors: string[];
  detected_columns: Record<string, string>;
  new_count: number;
  duplicate_count: number;
  uncategorised_count: number;
}

export interface CommitResult {
  created: number;
  skipped_duplicates: number;
  errors: string[];
}

export interface Merchant {
  id: number;
  canonical_name: string;
  default_category_id: number | null;
  transaction_count: number;
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
