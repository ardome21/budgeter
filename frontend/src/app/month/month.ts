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

  // ---- budget editing -------------------------------------------------
  editing = signal(false);
  drafts = signal<Partial<Record<number, string>>>({});
  savingBudget = signal(false);
  allCategories = signal<{ id: number; name: string }[]>([]);

  draftTotal = computed(() =>
    Object.values(this.drafts())
      .reduce((sum: number, v) => sum + (Number(v) || 0), 0)
      .toFixed(2),
  );

  startEditing(): void {
    const seeded: Record<number, string> = {};
    for (const line of this.summary()?.categories ?? []) {
      if (Number(line.allocated) > 0) seeded[line.category_id] = line.allocated;
    }
    this.drafts.set(seeded);
    this.editing.set(true);
    if (!this.allCategories().length) {
      this.api.categories().subscribe((c) => this.allCategories.set(c));
    }
  }

  setDraft(categoryId: number, value: string): void {
    this.drafts.update((d) => ({ ...d, [categoryId]: value }));
  }

  cancelEditing(): void {
    this.editing.set(false);
    this.drafts.set({});
  }

  saveBudget(): void {
    const [year, month] = this.selected().split('-').map(Number);
    const allocations = Object.entries(this.drafts())
      .filter((e): e is [string, string] => !!e[1] && Number(e[1]) > 0)
      .map(([id, v]) => ({ category_id: Number(id), amount: v }));

    this.savingBudget.set(true);
    this.api.setAllocations(year, month, allocations).subscribe({
      next: () => {
        this.savingBudget.set(false);
        this.cancelEditing();
        this.load();
      },
      error: (e) => {
        this.savingBudget.set(false);
        this.error.set(this.describe(e));
      },
    });
  }

  /** Most months are last month with a number or two changed. */
  copyPrevious(): void {
    const [year, month] = this.selected().split('-').map(Number);
    const previous = this.periods().find(
      (p) => !(p.year === year && p.month === month),
    );
    if (!previous) return;
    this.savingBudget.set(true);
    this.api
      .copyAllocations(year, month, previous.year, previous.month)
      .subscribe({
        next: () => {
          this.savingBudget.set(false);
          this.cancelEditing();
          this.load();
        },
        error: (e) => {
          this.savingBudget.set(false);
          this.error.set(this.describe(e));
        },
      });
  }

  previousLabel(): string {
    const [year, month] = this.selected().split('-').map(Number);
    const p = this.periods().find(
      (x) => !(x.year === year && x.month === month),
    );
    return p ? `${MONTH_NAMES[p.month - 1]} ${p.year}` : '';
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
