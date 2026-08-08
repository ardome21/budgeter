import { DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import { MerchantField } from '../merchant-field/merchant-field';
import {
  Category,
  PlaidStatus,
  SyncCommitResult,
  SyncResult,
  SyncRow,
  formatMoney,
} from '../models';

/** A synced row plus the choices made about it on screen. */
interface ReviewRow extends SyncRow {
  categoryId: number | null;
  include: boolean;
  recurring: boolean;
}

/** Plaid Link, injected into the page by the script tag in index.html. */
declare const Plaid: {
  create(options: {
    token: string;
    onSuccess: (publicToken: string) => void;
    onExit: (err: unknown) => void;
  }): { open(): void };
};

/**
 * Linked banks: refresh instead of exporting a CSV.
 *
 * Same rule as the Import screen — preview first, commit second. A bank feed
 * guesses a merchant and a category exactly as a CSV does, and a guess that
 * writes itself is a guess nobody checks.
 *
 * What the bank *revises* is not shown for review, only counted. A pending
 * charge that posts at a different amount is the bank settling its own record,
 * and holding that behind a prompt would leave the ledger knowingly wrong until
 * someone clicked.
 */
@Component({
  selector: 'app-linked',
  imports: [DatePipe, FormsModule, MerchantField, RouterLink],
  templateUrl: './linked.html',
  styleUrl: './linked.scss',
})
export class Linked {
  private api = inject(Api);

  status = signal<PlaidStatus | null>(null);
  categories = signal<Category[]>([]);
  sync = signal<SyncResult | null>(null);
  review = signal<ReviewRow[]>([]);
  result = signal<SyncCommitResult | null>(null);
  busy = signal(false);
  linking = signal(false);
  error = signal<string | null>(null);

  readonly fmt = formatMoney;

  includedCount = computed(() => this.review().filter((r) => r.include).length);
  needsCategory = computed(
    () => this.review().filter((r) => r.include && !r.categoryId).length,
  );
  flaggedCount = computed(
    () =>
      this.review().filter((r) => r.include && r.near_duplicates.length).length,
  );
  canCommit = computed(
    () => this.includedCount() > 0 && this.needsCategory() === 0,
  );
  hasItems = computed(() => (this.status()?.items.length ?? 0) > 0);

  constructor() {
    this.api.categories().subscribe((c) => this.categories.set(c));
    this.load();
  }

  private load(): void {
    this.api.plaidStatus().subscribe({
      next: (s) => this.status.set(s),
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  // --- Linking -------------------------------------------------------------

  /** Open Plaid Link. `itemId` re-authenticates an existing bank instead of
   *  adding a second copy of it. */
  link(itemId: number | null = null): void {
    if (typeof Plaid === 'undefined') {
      this.error.set(
        'Plaid Link did not load. Check the network — it is fetched from cdn.plaid.com.',
      );
      return;
    }
    this.linking.set(true);
    this.error.set(null);
    this.api.linkToken(itemId).subscribe({
      next: ({ link_token }) => {
        Plaid.create({
          token: link_token,
          onSuccess: (publicToken) => {
            this.api.exchangePublicToken(publicToken).subscribe({
              next: () => {
                this.linking.set(false);
                this.load();
              },
              error: (e) => {
                this.error.set(this.describe(e));
                this.linking.set(false);
              },
            });
          },
          onExit: () => this.linking.set(false),
        }).open();
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.linking.set(false);
      },
    });
  }

  unlink(id: number, name: string): void {
    if (
      !confirm(
        `Stop syncing ${name}?\n\nIts accounts and every transaction already ` +
          `imported stay exactly where they are — only the connection goes.`,
      )
    )
      return;
    this.api.unlinkItem(id).subscribe({
      next: () => this.load(),
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  rewind(id: number): void {
    this.api.rewindItem(id).subscribe({
      next: () => this.load(),
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  // --- Refresh -------------------------------------------------------------

  refresh(): void {
    this.busy.set(true);
    this.error.set(null);
    this.result.set(null);
    this.api.syncLinked().subscribe({
      next: (s) => {
        this.sync.set(s);
        this.review.set(
          s.rows.map((r) => ({
            ...r,
            categoryId: r.suggested_category_id,
            recurring: r.is_recurring,
            // Ticked by default, near-duplicates included. A near match is a
            // question, not a verdict, and quietly dropping real spending is
            // the worse failure.
            include: true,
          })),
        );
        this.busy.set(false);
        this.load();
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.busy.set(false);
      },
    });
  }

  setMerchant(row: ReviewRow, value: string | null): void {
    this.review.update((rows) =>
      rows.map((r) => (r.key === row.key ? { ...r, merchant_key: value } : r)),
    );
  }

  setCategory(row: ReviewRow, value: string | number): void {
    this.review.update((rows) =>
      rows.map((r) =>
        r.key === row.key ? { ...r, categoryId: Number(value) } : r,
      ),
    );
  }

  /** Committed or flexible. Suggested from the fixed-cost list, correctable
   *  here — the gym is a commitment that lives in a discretionary category. */
  toggleRecurring(row: ReviewRow): void {
    this.review.update((rows) =>
      rows.map((r) => (r.key === row.key ? { ...r, recurring: !r.recurring } : r)),
    );
  }

  toggle(row: ReviewRow): void {
    this.review.update((rows) =>
      rows.map((r) => (r.key === row.key ? { ...r, include: !r.include } : r)),
    );
  }

  fillBlanks(value: string): void {
    if (!value) return;
    this.review.update((rows) =>
      rows.map((r) => (r.categoryId ? r : { ...r, categoryId: Number(value) })),
    );
  }

  untickFlagged(): void {
    this.review.update((rows) =>
      rows.map((r) => (r.near_duplicates.length ? { ...r, include: false } : r)),
    );
  }

  commit(): void {
    const rows = this.review()
      .filter((r) => r.include && r.categoryId)
      .map((r) => ({
        key: r.key,
        account_id: r.account_id,
        occurred_on: r.occurred_on,
        raw_description: r.raw_description,
        amount: r.amount,
        category_id: r.categoryId,
        is_recurring: r.recurring,
        // Sent even when unchanged, so what was reviewed is what lands rather
        // than the backend guessing a second time.
        merchant_key: r.merchant_key ?? '',
      }));
    if (!rows.length) return;

    this.busy.set(true);
    this.error.set(null);
    this.api.commitLinked(rows).subscribe({
      next: (res) => {
        this.result.set(res);
        this.sync.set(null);
        this.review.set([]);
        this.busy.set(false);
        this.load();
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.busy.set(false);
      },
    });
  }

  discard(): void {
    this.sync.set(null);
    this.review.set([]);
    this.error.set(null);
  }

  private describe(e: unknown): string {
    const err = e as { status?: number; error?: { detail?: unknown } };
    if (err?.status === 0)
      return 'Cannot reach the API. Is the backend running?';
    const detail = err?.error?.detail;
    return typeof detail === 'string' ? detail : 'Something went wrong.';
  }
}
