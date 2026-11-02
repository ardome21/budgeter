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
  /** Rows filed here. Delete is only offered at zero — the server refuses
   *  otherwise, and the button should not promise what it cannot do. */
  transaction_count: number;
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
  /** Authorised, not posted. In no total until it posts. */
  pending: boolean;
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

export interface BudgetPlanRow {
  category_id: number;
  category_name: string;
  /** In force now — this month's own, or carried forward. Null is no target. */
  budget: string | null;
  last_month: string;
  /** The median of the last three complete months, not the mean. */
  typical: string;
  /** How many of those three months it appeared in. Seen once in three is a
   *  different proposal from seen every month. */
  months_seen: number;
  /** Rounded up to the next $10 on the server. Null where nothing was spent. */
  from_last_month: string | null;
  from_typical: string | null;
}

export interface BudgetPlan {
  month: string;
  /** Which month set the budget in force; null when none ever has. */
  budget_from: string | null;
  last_month: string;
  last_month_spent: string;
  last_month_income: string;
  typical_spent: string;
  rows: BudgetPlanRow[];
  /** Income categories, positive, with proposals rounded down rather than up. */
  income: BudgetPlanRow[];
}

/** A charge that has come in each of the last three months. */
export interface RegularCharge {
  merchant: string;
  amount: string;
  day: number;
  category_id: number;
  category_name: string;
  /** Filed by your merchant rule rather than the bank's label. */
  ruled: boolean;
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
  /** Spending only: last month, to the same day while this one runs. */
  previous: string | null;
  /** This month less `previous`, worked out on the server. */
  change: string | null;
  /** The few merchants that make up most of it. */
  merchants: MerchantSpend[];
}

export interface MerchantSpend {
  name: string;
  spent: string;
  count: number;
}

export interface BigPurchase {
  id: number;
  occurred_on: string;
  merchant: string;
  category_name: string;
  amount: string;
}

/** A charge that has come in each of the last three months, not yet this one. */
export interface UpcomingCharge {
  merchant: string;
  category_name: string;
  amount: string;
  expected_on: string;
  /** Its day has passed and nothing like it has posted. */
  late: boolean;
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
  /** The income budget in force, positive. Null when none is set. */
  income_expected: string | null;
  /** Money in less spending, as it stands. */
  left: string;
  /** Once the expected pay lands: the larger of received and expected, less
   *  spending. Null without an income budget. */
  left_expected: string | null;
  /** Expected pay less the spending budget: what the plan leaves. */
  left_planned: string | null;
  /** The month whose budget applies: this one, or the last one set before it.
   *  Null when no budget has ever been set. */
  budget_from: string | null;
  categories: CategorySpend[];
  transaction_count: number;
  previous_month: string;
  /** The day of last month the comparison runs to; null for all of it. */
  compared_through: number | null;
  previous_spent: string;
  /** Spending so far at the end of each day: this month through today. */
  daily: string[];
  /** The same, for the whole of last month. */
  previous_daily: string[];
  biggest: BigPurchase[];
  upcoming: UpcomingCharge[];
  upcoming_total: string;
  /** Spending authorised but not posted, in no other figure. */
  pending_total: string;
  pending_count: number;
  /** Left, less what is pending and what is still to come. */
  left_after_upcoming: string;
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
  accounts: LinkedAccount[];
}

export interface LinkedAccount {
  id: number;
  name: string;
  mask: string | null;
  group: AccountGroup;
  /** The bank's own latest figure, never a rebuilt one. */
  balance: string | null;
  /** Set when hidden. */
  closed_on: string | null;
  newest_transaction: string | null;
  oldest_transaction: string | null;
  transaction_count: number;
  new_this_week: number;
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

// --- the overview -------------------------------------------------------------

export type PayLineKind = 'TAX' | 'SAVING' | 'DEDUCTION' | 'EMPLOYER';

export interface PayLine {
  kind: PayLineKind;
  name: string;
  amount: string;
  /** The employer plan a saving line pays into. Whatever else lands there is the employer's. */
  account_id: number | null;
}

/** One paycheck as the stub reads, in force from `effective_from` on. */
export interface PayStub {
  effective_from: string;
  periods_per_year: number;
  gross: string;
  lines: PayLine[];
  /** Per paycheck, summed by the server. */
  tax: string;
  saving: string;
  deduction: string;
  employer: string;
  take_home: string;
}

export type StandingBucket = 'cash' | 'invested' | 'retirement' | 'owed';

export interface StandingAccount {
  account_id: number;
  name: string;
  institution: string;
  bucket: StandingBucket;
  amount: string;
  as_of: string;
}

export interface FlowMonth {
  month: string;
  /** Still running. */
  partial: boolean;
  /** Starts before every bank's history does, so some spending is missing. */
  missing_history: boolean;
  take_home: string;
  spent: string;
  kept: string;
  /** A pay stub covers this month. Without one the pre-paycheck fields are null. */
  has_stub: boolean;
  earned: string | null;
  tax: string | null;
  saved_from_pay: string | null;
  employer: string | null;
  deductions: string | null;
  saved: string;
  /** "0.183" is 18.3%. */
  savings_rate: string | null;
}

export interface Typical {
  take_home: string | null;
  spent: string | null;
  kept: string | null;
  earned: string | null;
  saved: string | null;
  saved_from_pay: string | null;
  employer: string | null;
  savings_rate: string | null;
}

export interface BreakdownGroup {
  kind: PayLineKind;
  total: string;
  lines: { name: string; amount: string }[];
}

/** A typical month's pay, line by line. Every figure is positive; the screen supplies signs. */
export interface PayBreakdown {
  paychecks_per_month: string;
  gross_per_paycheck: string;
  gross: string;
  groups: BreakdownGroup[];
  take_home_pay: string;
  /** What reached the bank beyond the stub's take-home: interest, rounding. */
  other_income: string;
  reached_bank: string;
  /** gross + other_income. */
  earned: string;
}

export interface Overview {
  as_of: string | null;
  net_worth: string;
  cash: string;
  invested: string;
  retirement: string;
  /** Negative. */
  owed: string;
  accounts: StandingAccount[];
  /** Cash less what is owed on the cards. */
  cushion: string;
  /** Typical months of spending the cushion would pay for. */
  months_covered: string | null;
  typical_months: string[];
  typical: Typical;
  /** Null until every typical month has a pay stub. */
  pay_breakdown: PayBreakdown | null;
  /** Newest first. */
  flow: FlowMonth[];
  history_start: string | null;
}

// --- goals --------------------------------------------------------------------

export interface GoalInput {
  name: string;
  /** One or the other: dollars, or months of typical spending. */
  amount: string | null;
  months_of_spending: string | null;
  target_date: string | null;
}

export interface GoalRow extends GoalInput {
  id: number;
  /** In dollars, whichever way the goal is sized. */
  target: string;
  covered_cash: string;
  covered_invested: string;
  remaining: string;
  /** Dated goals only. */
  monthly_needed: string | null;
  /** The month it would be reached at today's pace; null if not within fifty years. */
  reached_on: string | null;
  on_track: boolean;
}

export interface GoalPlan {
  cash: string;
  invested: string;
  unassigned_cash: string;
  unassigned_invested: string;
  monthly_saving: string;
  monthly_kept: string;
  monthly_from_pay: string;
  typical_spent: string;
  monthly_needed: string;
  short_by: string;
  goals: GoalRow[];
}
