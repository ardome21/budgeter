import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import {
  MONTH_NAMES,
  MonthSummary,
  Overview,
  Period,
  formatMoney,
} from '../models';

@Component({
  selector: 'app-month',
  imports: [FormsModule],
  templateUrl: './month.html',
  styleUrl: './month.scss',
})
export class Month {
  private api = inject(Api);

  periods = signal<Period[]>([]);
  summary = signal<MonthSummary | null>(null);
  overview = signal<Overview | null>(null);
  selected = signal<string>('');
  loading = signal(true);
  error = signal<string | null>(null);

  readonly fmt = formatMoney;
  readonly monthNames = MONTH_NAMES;

  /** Categories over budget, which is the part worth surfacing. */
  overspent = computed(() =>
    (this.summary()?.categories ?? []).filter(
      (c) => Number(c.allocated) > 0 && Number(c.remaining) < 0,
    ),
  );

  unbudgeted = computed(() =>
    (this.summary()?.categories ?? []).filter((c) => Number(c.allocated) === 0),
  );

  constructor() {
    this.api.overview().subscribe({
      next: (o) => this.overview.set(o),
      error: () => {},
    });
    this.api.periods().subscribe({
      next: (rows) => {
        this.periods.set(rows);
        if (rows.length) {
          this.selected.set(`${rows[0].year}-${rows[0].month}`);
          this.load();
        } else {
          this.loading.set(false);
        }
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.loading.set(false);
      },
    });
  }

  onSelect(value: string): void {
    this.selected.set(value);
    this.load();
  }

  load(): void {
    const [year, month] = this.selected().split('-').map(Number);
    if (!year || !month) return;
    this.loading.set(true);
    this.error.set(null);
    this.api.summary(year, month).subscribe({
      next: (s) => {
        this.summary.set(s);
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.summary.set(null);
        this.loading.set(false);
      },
    });
  }

  label(p: Period): string {
    return `${MONTH_NAMES[p.month - 1]} ${p.year}`;
  }

  /** Bar width as a percentage, capped so a 400% overspend stays readable. */
  barWidth(pct: number | null): number {
    if (pct === null) return 0;
    return Math.min(Math.max(pct * 100, 0), 100);
  }

  private describe(e: unknown): string {
    const err = e as { status?: number; error?: { detail?: string } };
    if (err?.status === 404) return 'No data for that month yet.';
    if (err?.status === 0)
      return 'Cannot reach the API. Is the backend running?';
    return err?.error?.detail ?? 'Something went wrong loading this month.';
  }
}
