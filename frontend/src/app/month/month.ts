import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Api } from '../api';
import { CategorySpend, MonthSummary } from '../models';
import { Sync } from '../sync';

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
  imports: [RouterLink],
  templateUrl: './month.html',
  styleUrl: './month.scss',
})
export class Month {
  private api = inject(Api);
  private sync = inject(Sync);

  months = signal<string[]>([]);
  selected = signal<string | null>(null);
  summary = signal<MonthSummary | null>(null);
  loading = signal(true);
  error = signal<string | null>(null);

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

  private thisMonth(): string {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-01`;
  }
}
