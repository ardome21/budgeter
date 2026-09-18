import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';

import { Api } from '../api';
import { MerchantField } from '../merchant-field/merchant-field';
import { AccountRow, Category, Transaction } from '../models';

/**
 * The ledger, and the three things about a row that are yours to change.
 *
 * Category, merchant and notes are answers to a row. The amount, the date and
 * the account are the bank's account of what happened, and there is no control
 * here for any of them — correcting a guess is a different act from rewriting
 * a fact, and a ledger you can edit to say anything is not a ledger.
 *
 * Edits save on change rather than behind a Save button: every one of them is
 * a single field on a single row, and a button would only add a step in which
 * the change can be lost.
 */
@Component({
  selector: 'app-transactions',
  imports: [FormsModule, MerchantField],
  templateUrl: './transactions.html',
  styleUrl: './transactions.scss',
})
export class Transactions {
  private api = inject(Api);
  private route = inject(ActivatedRoute);

  rows = signal<Transaction[]>([]);
  total = signal(0);
  categories = signal<Category[]>([]);
  accounts = signal<AccountRow[]>([]);
  loading = signal(true);
  saving = signal<number | null>(null);

  month = signal<string>('');
  categoryId = signal<number | null>(null);
  accountId = signal<number | null>(null);
  search = signal('');

  private readonly pageSize = 200;
  offset = signal(0);

  constructor() {
    this.api.categories().subscribe((c) => this.categories.set(c));
    this.api.accounts().subscribe((a) => this.accounts.set(a));

    // Arriving from a category on the Month screen should land on that
    // category's rows, not on everything.
    this.route.queryParamMap.subscribe((params) => {
      this.month.set(params.get('month') ?? '');
      const category = params.get('category');
      this.categoryId.set(category ? Number(category) : null);
      this.load();
    });
  }

  load(): void {
    this.loading.set(true);
    this.api
      .transactions({
        month: this.month() || undefined,
        category_id: this.categoryId() ?? undefined,
        account_id: this.accountId() ?? undefined,
        search: this.search() || undefined,
        limit: this.pageSize,
        offset: this.offset(),
      })
      .subscribe({
        next: (page) => {
          this.rows.set(page.rows);
          this.total.set(page.total);
          this.loading.set(false);
        },
        error: () => this.loading.set(false),
      });
  }

  filter(): void {
    this.offset.set(0);
    this.load();
  }

  page(by: number): void {
    this.offset.update((o) => Math.max(0, o + by * this.pageSize));
    this.load();
  }

  refile(row: Transaction, categoryId: number): void {
    this.saving.set(row.id);
    this.api.updateTransaction(row.id, { category_id: Number(categoryId) }).subscribe({
      next: (updated) => this.replace(updated),
      error: () => this.saving.set(null),
    });
  }

  rename(row: Transaction, merchant: string): void {
    if (merchant === (row.merchant ?? '')) return;
    this.saving.set(row.id);
    this.api.updateTransaction(row.id, { merchant }).subscribe({
      next: (updated) => this.replace(updated),
      error: () => this.saving.set(null),
    });
  }

  annotate(row: Transaction, notes: string): void {
    if (notes === (row.notes ?? '')) return;
    this.saving.set(row.id);
    this.api.updateTransaction(row.id, { notes }).subscribe({
      next: (updated) => this.replace(updated),
      error: () => this.saving.set(null),
    });
  }

  /** Money out is positive in storage. Shown as a negative here, because a
   * column of unsigned numbers where some are refunds reads as all spending. */
  display(row: Transaction): string {
    return row.amount.startsWith('-') ? `+${row.amount.slice(1)}` : row.amount;
  }

  isCredit(row: Transaction): boolean {
    return row.amount.startsWith('-');
  }

  day(iso: string): string {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
    });
  }

  private replace(updated: Transaction): void {
    this.rows.update((rows) =>
      rows.map((r) => (r.id === updated.id ? updated : r)),
    );
    this.saving.set(null);
  }
}
