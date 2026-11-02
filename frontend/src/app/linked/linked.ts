import { Component, effect, inject, signal, untracked } from '@angular/core';

import { Api } from '../api';
import { LinkedItem } from '../models';
import { Sync } from '../sync';

/** Plaid Link, injected into the page by the script tag in index.html. */
declare const Plaid: {
  create(options: {
    token: string;
    onSuccess: (publicToken: string) => void;
    onExit: (err: unknown) => void;
  }): { open(): void };
};

/**
 * The banks themselves: which are linked, when each was last asked, and the
 * ones that need you to sign in again. Refreshing lives in the header.
 */
@Component({
  selector: 'app-linked',
  imports: [],
  templateUrl: './linked.html',
  styleUrl: './linked.scss',
})
export class Linked {
  private api = inject(Api);
  sync = inject(Sync);

  items = signal<LinkedItem[]>([]);
  linking = signal(false);
  error = signal<string | null>(null);

  constructor() {
    this.load();
    effect(() => {
      if (this.sync.version()) untracked(() => this.load());
    });
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
            // Update mode signs an existing item back in; there is nothing to
            // exchange, and exchanging anyway is refused as already linked.
            if (itemId !== null) {
              this.linking.set(false);
              this.sync.run();
              return;
            }
            this.api.exchange(publicToken, products).subscribe({
              next: () => {
                this.linking.set(false);
                this.load();
                this.sync.run();
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

  hasInvestments(item: LinkedItem): boolean {
    return item.products.includes('investments');
  }

  when(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : 'never';
  }

  private describe(err: unknown): string {
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    return detail ?? 'Something went wrong talking to the bank.';
  }
}
