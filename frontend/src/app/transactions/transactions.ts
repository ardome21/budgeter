import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import {
  Category,
  MONTH_NAMES,
  NewTransaction,
  Period,
  Transaction,
  formatMoney,
} from '../models';

interface EntryForm {
  occurred_on: string;
  raw_description: string;
  category_id: number;
  amount: string;
  is_recurring: boolean;
}

@Component({
  selector: 'app-transactions',
  imports: [FormsModule],
  templateUrl: './transactions.html',
  styleUrl: './transactions.scss',
})
export class Transactions {
  private api = inject(Api);

  rows = signal<Transaction[]>([]);
  categories = signal<Category[]>([]);
  periods = signal<Period[]>([]);
  loading = signal(true);
  saving = signal(false);
  error = signal<string | null>(null);
  justAdded = signal<number | null>(null);

  filterPeriod = signal<string>('');
  search = signal<string>('');

  readonly fmt = formatMoney;
  readonly monthNames = MONTH_NAMES;

  // The entry form. Defaults to today so the common case is two fields.
  form = signal<EntryForm>({
    occurred_on: new Date().toISOString().slice(0, 10),
    raw_description: '',
    category_id: 0,
    amount: '',
    is_recurring: false,
  });

  /** Templates cannot spread, so field updates live here. */
  setField<K extends keyof EntryForm>(key: K, value: EntryForm[K]): void {
    this.form.update((f) => ({ ...f, [key]: value }));
  }

  constructor() {
    // Deliberately no default category. The first category by sort order is
    // Savings, and silently filing a grocery run under Savings is worse than
    // making the field an explicit choice.
    this.api.categories().subscribe((c) => this.categories.set(c));
    this.api.periods().subscribe((p) => {
      this.periods.set(p);
      if (p.length) {
        this.filterPeriod.set(`${p[0].year}-${p[0].month}`);
      }
      this.reload();
    });
  }

  reload(): void {
    this.loading.set(true);
    const [year, month] = this.filterPeriod()
      ? this.filterPeriod().split('-').map(Number)
      : [undefined, undefined];
    this.api
      .transactions({ year, month, q: this.search() || undefined, limit: 500 })
      .subscribe({
        next: (rows) => {
          this.rows.set(rows);
          this.loading.set(false);
        },
        error: (e) => {
          this.error.set(this.describe(e));
          this.loading.set(false);
        },
      });
  }

  onFilterChange(value: string): void {
    this.filterPeriod.set(value);
    this.reload();
  }

  onSearch(value: string): void {
    this.search.set(value);
    this.reload();
  }

  add(): void {
    const f = this.form();
    if (!f.raw_description.trim() || !f.amount.trim() || !f.category_id) {
      this.error.set('Description, amount and category are all required.');
      return;
    }
    this.saving.set(true);
    this.error.set(null);

    const body: NewTransaction = {
      occurred_on: f.occurred_on || null,
      raw_description: f.raw_description.trim(),
      category_id: Number(f.category_id),
      amount: f.amount.trim(),
      is_recurring: f.is_recurring,
    };
    // An undated entry still needs a month, or it cannot be rolled up.
    if (!f.occurred_on) {
      const [year, month] = (this.filterPeriod() || '').split('-').map(Number);
      if (year && month) {
        body.year = year;
        body.month = month;
      }
    }

    this.api.createTransaction(body).subscribe({
      next: (txn) => {
        this.saving.set(false);
        this.justAdded.set(txn.id);
        this.form.update((prev) => ({
          ...prev,
          raw_description: '',
          amount: '',
          is_recurring: false,
        }));
        this.reload();
        setTimeout(() => this.justAdded.set(null), 2500);
      },
      error: (e) => {
        this.saving.set(false);
        this.error.set(this.describe(e));
      },
    });
  }

  recategorise(txn: Transaction, categoryId: string): void {
    this.api
      .updateTransaction(txn.id, { category_id: Number(categoryId) })
      .subscribe({
        next: (updated) =>
          this.rows.update((rows) =>
            rows.map((r) => (r.id === updated.id ? updated : r)),
          ),
        error: (e) => this.error.set(this.describe(e)),
      });
  }

  remove(txn: Transaction): void {
    if (
      !confirm(`Delete "${txn.raw_description}" for ${this.fmt(txn.amount)}?`)
    )
      return;
    this.api.deleteTransaction(txn.id).subscribe({
      next: () =>
        this.rows.update((rows) => rows.filter((r) => r.id !== txn.id)),
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  periodLabel(p: Period): string {
    return `${MONTH_NAMES[p.month - 1]} ${p.year}`;
  }

  private describe(e: unknown): string {
    const err = e as { status?: number; error?: { detail?: unknown } };
    if (err?.status === 0)
      return 'Cannot reach the API. Is the backend running?';
    const detail = err?.error?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail) && detail.length) {
      const first = detail[0] as { msg?: string; loc?: string[] };
      return `${first.loc?.slice(-1)[0] ?? 'field'}: ${first.msg ?? 'invalid'}`;
    }
    return 'Something went wrong.';
  }
}
