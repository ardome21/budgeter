import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import { Budget, Category, CategoryKind } from '../models';

/**
 * Categories, and what each is meant to cost this month.
 *
 * Budgets are edited as one table and saved once, which is why the endpoint
 * takes the whole month rather than a row at a time. The Copy button exists
 * because budgets went unmaintained for a year in the version this replaces,
 * and the reason was that every month started empty — carrying last month
 * forward makes keeping them current an edit rather than a re-entry.
 */
@Component({
  selector: 'app-settings',
  imports: [FormsModule],
  templateUrl: './settings.html',
  styleUrl: './settings.scss',
})
export class Settings {
  private api = inject(Api);

  categories = signal<Category[]>([]);
  amounts = signal<Record<number, string>>({});
  month = signal(this.currentMonth());
  saved = signal(false);
  error = signal<string | null>(null);

  newName = signal('');
  newKind = signal<CategoryKind>('SPENDING');

  readonly kinds: CategoryKind[] = ['SPENDING', 'INCOME', 'TRANSFER', 'SAVINGS'];

  constructor() {
    this.load();
  }

  load(): void {
    this.api.categories().subscribe((c) => this.categories.set(c));
    const [year, month] = this.month().split('-').map(Number);
    this.api.budgets(year, month).subscribe((rows) => this.fill(rows));
  }

  changeMonth(value: string): void {
    this.month.set(value);
    this.load();
  }

  save(): void {
    const [year, month] = this.month().split('-').map(Number);
    const rows: Budget[] = Object.entries(this.amounts())
      // A blank is "no target", which is a different statement from zero and
      // is expressed by leaving the category out entirely.
      .filter(([, amount]) => amount !== '' && amount !== null)
      .map(([id, amount]) => ({ category_id: Number(id), amount }));

    this.api.setBudgets(year, month, rows).subscribe({
      next: () => {
        this.saved.set(true);
        setTimeout(() => this.saved.set(false), 2000);
      },
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  copyForward(): void {
    const [year, month] = this.month().split('-').map(Number);
    this.api.copyBudgets(year, month).subscribe({
      next: (rows) => this.fill(rows),
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  addCategory(): void {
    const name = this.newName().trim();
    if (!name) return;
    this.api
      .createCategory({ name, kind: this.newKind(), sort_order: 100 })
      .subscribe({
        next: () => {
          this.newName.set('');
          this.load();
        },
        error: (e) => this.error.set(this.describe(e)),
      });
  }

  removeCategory(category: Category): void {
    if (!confirm(`Delete ${category.name}?`)) return;
    this.api.deleteCategory(category.id).subscribe({
      next: () => this.load(),
      // Refused while transactions are filed here, and the message says how
      // many — which is the useful half of the answer.
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  amountFor(id: number): string {
    return this.amounts()[id] ?? '';
  }

  setAmount(id: number, value: string): void {
    this.amounts.update((a) => ({ ...a, [id]: value }));
  }

  private fill(rows: Budget[]): void {
    const next: Record<number, string> = {};
    for (const row of rows) next[row.category_id] = row.amount;
    this.amounts.set(next);
  }

  private currentMonth(): string {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  }

  private describe(err: unknown): string {
    return (
      (err as { error?: { detail?: string } })?.error?.detail ??
      'That did not save.'
    );
  }
}
