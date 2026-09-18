import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Api } from '../api';
import { CategorySpend, MonthSummary } from '../models';

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
    this.api.months().subscribe({
      next: (months) => {
        this.months.set(months);
        this.select(months[0] ?? this.thisMonth());
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

  isOver(row: CategorySpend): boolean {
    return row.remaining !== null && Number(row.remaining) < 0;
  }

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
