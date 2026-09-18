import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import { LinkedItem, Preview, PreviewRow } from '../models';

/** Plaid Link, injected into the page by the script tag in index.html. */
declare const Plaid: {
  create(options: {
    token: string;
    onSuccess: (publicToken: string) => void;
    onExit: (err: unknown) => void;
  }): { open(): void };
};

interface Reviewable extends PreviewRow {
  include: boolean;
}

/**
 * Linked banks: refresh instead of visiting five websites.
 *
 * Preview first, commit second. A bank feed guesses a merchant and a category
 * exactly as an import does, and a guess that writes itself is a guess nobody
 * checks. The cursor moves only on commit, so closing this screen without
 * acting re-offers the same rows next time rather than stepping past them.
 *
 * What the bank *revises* is not offered for review, only counted. A pending
 * charge that posts at a different amount is the institution settling its own
 * record — holding that behind a prompt leaves the ledger knowingly wrong
 * until somebody clicks.
 */
@Component({
  selector: 'app-linked',
  imports: [FormsModule],
  templateUrl: './linked.html',
  styleUrl: './linked.scss',
})
export class Linked {
  private api = inject(Api);

  items = signal<LinkedItem[]>([]);
  rows = signal<Reviewable[]>([]);
  active = signal<LinkedItem | null>(null);
  result = signal<Preview | null>(null);
  busy = signal(false);
  linking = signal(false);
  error = signal<string | null>(null);

  constructor() {
    this.load();
  }

  load(): void {
    this.api.linkedItems().subscribe({
      next: (items) => this.items.set(items),
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  /**
   * Open Plaid Link.
   *
   * `products` is the decision that cannot be revisited afterwards. An item is
   * created with a set of products and keeps it: adding investments to a bank
   * linked for transactions means running Link again with fresh consent, and a
   * brokerage linked without it returns balances and no holdings — which looks
   * like an empty portfolio rather than a mislink.
   */
  link(products: string[], itemId: number | null = null): void {
    if (typeof Plaid === 'undefined') {
      this.error.set(
        'Plaid Link did not load. Check the network — it is fetched from cdn.plaid.com.',
      );
      return;
    }
    this.linking.set(true);
    this.error.set(null);
    this.api.linkToken(products, itemId ?? undefined).subscribe({
      next: ({ link_token }) => {
        Plaid.create({
          token: link_token,
          onSuccess: (publicToken) => {
            this.api.exchange(publicToken, products).subscribe({
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

  refresh(item: LinkedItem): void {
    this.busy.set(true);
    this.error.set(null);
    this.active.set(item);
    this.result.set(null);
    this.api.preview(item.id).subscribe({
      next: (preview) => {
        this.result.set(preview);
        // Everything arrives ticked. Dropping real spending silently is worse
        // than a row you can see and untick.
        this.rows.set(preview.rows.map((r) => ({ ...r, include: true })));
        this.busy.set(false);
        this.load();
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.busy.set(false);
      },
    });
  }

  commit(): void {
    const item = this.active();
    if (!item) return;
    this.busy.set(true);
    const accept = this.rows()
      .filter((r) => r.include)
      .map((r) => r.transaction_id);
    this.api.commit(item.id, accept).subscribe({
      next: (res) => {
        this.result.set(res);
        this.rows.set([]);
        this.busy.set(false);
        this.load();
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.busy.set(false);
      },
    });
  }

  rewind(item: LinkedItem): void {
    if (
      !confirm(
        `Re-offer every transaction ${item.institution_name} still has? ` +
          'Rows already committed are filtered out, so only missing ones come back.',
      )
    ) {
      return;
    }
    this.api.rewind(item.id).subscribe({ next: () => this.refresh(item) });
  }

  unlink(item: LinkedItem): void {
    if (
      !confirm(
        `Stop syncing ${item.institution_name}? Its accounts and their history stay.`,
      )
    ) {
      return;
    }
    this.api.unlink(item.id).subscribe({ next: () => this.load() });
  }

  toggleAll(include: boolean): void {
    this.rows.update((rows) => rows.map((r) => ({ ...r, include })));
  }

  selected(): number {
    return this.rows().filter((r) => r.include).length;
  }

  hasInvestments(item: LinkedItem): boolean {
    return item.products.includes('investments');
  }

  day(iso: string): string {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
    });
  }

  when(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : 'never';
  }

  private describe(err: unknown): string {
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    return detail ?? 'Something went wrong talking to the bank.';
  }
}
