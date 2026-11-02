import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Api } from '../api';
import { BudgetPlan, CategorySpend, MonthSummary } from '../models';
import { UsdPipe, usd } from '../usd';
import { Sync } from '../sync';
import { PaceChart } from './pace-chart';

/**
 * Where the money went, and how that compares to what you meant.
 *
 * The one rule this screen enforces on itself: a number in the Spent column is
 * spending. Transfers and income appear in their own row at the top and are
 * counted in nothing else — a credit card payment is last month's spending
 * moving between two accounts you own, and letting it into this column made
 * the biggest purchase of August 2026 a $207.45 row that described nothing.
 */
@Component({
  selector: 'app-month',
  imports: [RouterLink, UsdPipe, PaceChart],
  templateUrl: './month.html',
  styleUrl: './month.scss',
})
export class Month {
  private api = inject(Api);
  private sync = inject(Sync);

  months = signal<string[]>([]);
  selected = signal<string | null>(null);
  summary = signal<MonthSummary | null>(null);
  /** Fetched only when no budget applies, to offer last month as a start. */
  plan = signal<BudgetPlan | null>(null);
  loading = signal(true);
  error = signal<string | null>(null);
  /** Categories opened to show their merchants. */
  opened = signal<Set<number>>(new Set());

  /** Only categories money actually moved through, or that have a target. */
  spending = computed(() =>
    (this.summary()?.categories ?? []).filter((c) => c.kind === 'SPENDING'),
  );

  /** Everything else, so the columns never have to explain themselves. */
  other = computed(() =>
    (this.summary()?.categories ?? []).filter((c) => c.kind !== 'SPENDING'),
  );

  constructor() {
    this.loadMonths();
    // A refresh that lands while this screen is open updates it in place.
    effect(() => {
      if (this.sync.version()) untracked(() => this.loadMonths(this.selected()));
    });
  }

  private loadMonths(keep: string | null = null): void {
    this.api.months().subscribe({
      next: (months) => {
        this.months.set(months);
        this.select(keep || (months[0] ?? this.thisMonth()));
      },
      error: () => {
        this.error.set('Could not load months.');
        this.loading.set(false);
      },
    });
  }

  select(month: string): void {
    this.selected.set(month);
    this.loading.set(true);
    const [year, m] = month.split('-').map(Number);
    this.api.month(year, m).subscribe({
      next: (summary) => {
        this.summary.set(summary);
        this.loading.set(false);
        this.plan.set(null);
        // The setup card is only offered for this month or a later one: a
        // budget for a month already over judges nothing.
        if (!summary.budget_from && month >= this.thisMonth()) {
          this.api.budgetPlan(year, m).subscribe((p) => this.plan.set(p));
        }
      },
      error: () => {
        this.error.set('Could not load that month.');
        this.loading.set(false);
      },
    });
  }

  /**
   * Whether the month is still running, which is the only case where pace is
   * a question. A finished month is judged on its total and nothing else.
   */
  inProgress = computed(() => {
    const s = this.summary();
    return !!s && s.days_elapsed > 0 && s.days_elapsed < s.days_in_month;
  });

  /**
   * How far through the month we are, as a percentage for the pace marker.
   *
   * Days, not money — this is the one arithmetic the browser is allowed and it
   * is on a calendar, not on a balance.
   */
  monthProgress = computed(() => {
    const s = this.summary();
    if (!s || !s.days_in_month) return 0;
    return Math.min(100, (s.days_elapsed / s.days_in_month) * 100);
  });

  /**
   * A bar width, and the only place this screen does arithmetic on money.
   *
   * Safe because the result is a percentage for CSS, never a figure anybody
   * reads. Every number on screen is rendered exactly as the server sent it.
   */
  barWidth(row: CategorySpend): number {
    if (!row.budget) return 0;
    const spent = Number(row.spent);
    const budget = Number(row.budget);
    if (!budget) return 0;
    return Math.min(100, Math.round((spent / budget) * 100));
  }

  /**
   * Three states, and they are genuinely different claims.
   *
   * `over` — you have spent more than the whole month allows.
   * `ahead` — you are inside the budget but spending faster than it lasts.
   * `ok` — neither.
   *
   * All three are decided on the server in Decimal; this only names them. The
   * browser never compares two money strings, because it has no decimal type
   * and comparing "9.90" against "10.00" as text is wrong in both directions.
   */
  state(row: CategorySpend): 'over' | 'ahead' | 'ok' {
    if (row.over_budget) return 'over';
    if (row.over_pace && this.inProgress()) return 'ahead';
    return 'ok';
  }

  isOver(row: CategorySpend): boolean {
    return row.over_budget;
  }

  /** The headline, in the words someone would actually use. */
  verdict = computed(() => {
    const s = this.summary();
    if (!s || s.expected_to_date === null) return null;
    if (s.over_budget) return 'over budget';
    if (s.over_pace && this.inProgress()) return 'ahead of pace';
    return 'on track';
  });

  /** Dates from the API are plain calendar days. `new Date(iso)` reads them as
   * UTC midnight, which in this timezone renders as the previous day. */
  label(month: string): string {
    const [year, m] = month.split('-').map(Number);
    return new Date(year, m - 1, 1).toLocaleDateString(undefined, {
      month: 'long',
      year: 'numeric',
    });
  }

  toggle(id: number): void {
    this.opened.update((open) => {
      const next = new Set(open);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  /** "vs Oct", or "vs Oct 1–2" while the month is running. */
  compareHead = computed(() => {
    const s = this.summary();
    if (!s) return '';
    const name = this.shortMonth(s.previous_month);
    return s.compared_through
      ? `vs ${name} 1–${s.compared_through}`
      : `vs ${name}`;
  });

  compareTitle = computed(() => {
    const s = this.summary();
    if (!s) return '';
    return s.compared_through
      ? `Against the first ${s.compared_through} days of last month, so a month in progress is not set beside a whole one`
      : 'Against the whole of last month';
  });

  /** "+$8.95", "−$140.00", or "same". The sign is the server's; this only
   *  adds the plus that a positive string does not carry. */
  signed(change: string | null): string {
    if (change === null) return '—';
    if (/^-?0*\.?0*$/.test(change)) return 'same';
    return change.startsWith('-') ? usd(change)! : usd('+' + change)!;
  }

  /** Spending more than last month. Read off the sign, not compared. */
  isUp(change: string | null): boolean {
    return !!change && !change.startsWith('-') && !/^0*\.?0*$/.test(change);
  }

  /** "Nov 5" from a calendar day, read as local. */
  day(iso: string): string {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
    });
  }

  private shortMonth(month: string): string {
    const [year, m] = month.split('-').map(Number);
    return new Date(year, m - 1, 1).toLocaleDateString(undefined, { month: 'short' });
  }

  /** How many categories last month's spending touched — the rows the setup fills. */
  planCategories = computed(
    () => this.plan()?.rows.filter((r) => r.from_last_month !== null).length ?? 0,
  );

  /** "November", with no year — the card says which month without repeating it. */
  monthName(month: string): string {
    const [year, m] = month.split('-').map(Number);
    return new Date(year, m - 1, 1).toLocaleDateString(undefined, { month: 'long' });
  }

  /** "2026-11" for the Budget page's month parameter. */
  budgetMonth(): string {
    return (this.selected() ?? this.thisMonth()).slice(0, 7);
  }

  private thisMonth(): string {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-01`;
  }
}
