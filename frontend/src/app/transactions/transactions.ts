import { Component, effect, inject, signal, untracked } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';

import { Api } from '../api';
import { MerchantField } from '../merchant-field/merchant-field';
import { AccountRow, Category, Transaction } from '../models';
import { Sync } from '../sync';

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
  private sync = inject(Sync);
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

  /**
   * Whether re-filing a row carries the rest of that merchant with it.
   *
   * On by default, and that is the point: Uber Eats appears ten times, Rhino
   * Market nine, Apple nine. Filing each of them one row at a time is the
   * reason 71 transactions sat unfiled. Only rows still in Uncategorised move,
   * so a deliberate one-off filing is never overwritten.
   */
  applyToMerchant = signal(true);

  /** What the last bulk action did, shown until the next one. */
  note = signal<string | null>(null);
  busy = signal(false);

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

    // A refresh that lands while this screen is open updates it in place.
    effect(() => {
      if (this.sync.version()) untracked(() => this.load());
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
    this.api
      .updateTransaction(row.id, {
        category_id: Number(categoryId),
        apply_to_merchant: this.applyToMerchant(),
      })
      .subscribe({
        next: (updated) => {
          const also = updated.also_filed ?? 0;
          this.replace(updated);
          if (also) {
            this.note.set(
              `${also} other ${row.merchant} ${also === 1 ? 'row' : 'rows'} moved too.`,
            );
            // Those rows are on this page as well, so the table has to be told
            // — otherwise the change is real in the database and invisible
            // here, which reads as the action having done nothing.
            this.load();
          }
        },
        error: () => this.saving.set(null),
      });
  }

  /**
   * Run every unfiled row back through the resolver.
   *
   * The 71 rows that arrived before anything could file them: the merchant
   * history the suggester reads did not exist yet, and the bank's own label
   * was being discarded rather than stored. Both are fixed, so this is the
   * one-off that applies the fix to what is already here.
   */
  refileAll(): void {
    this.busy.set(true);
    this.note.set(null);
    this.api.refileUncategorised().subscribe({
      next: ({ moved, left }) => {
        this.note.set(
          left
            ? `${moved} filed. ${left} still need a category — file one row of ` +
              'each merchant and the rest follow.'
            : `${moved} filed. Nothing left unfiled.`,
        );
        this.busy.set(false);
        this.load();
      },
      error: () => {
        this.note.set('Could not re-file those rows.');
        this.busy.set(false);
      },
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

  /** Money out is positive in storage and stays unsigned here; a refund, which
   * is stored negative, is shown with a leading + and its own colour. A column
   * where refunds looked like charges read as all spending. */
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
