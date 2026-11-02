import { Component, effect, inject, signal, untracked } from '@angular/core';

import { Api } from '../api';
import { AccountGroup, LinkedAccount, LinkedItem } from '../models';
import { UsdPipe } from '../usd';
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
 * Is everything coming in? Each bank with each of its accounts: its balance,
 * its newest transaction and what the last week brought, so an account gone
 * quiet stands out against its siblings. Refreshing lives in the header.
 */
@Component({
  selector: 'app-linked',
  imports: [UsdPipe],
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

  toggleHidden(account: LinkedAccount): void {
    const closed_on = account.closed_on ? null : today();
    this.api.updateAccount(account.id, { closed_on }).subscribe({
      next: () => this.load(),
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  /**
   * A spending account with nothing for three weeks, while it is still
   * counted. Savings and brokerages go quiet for months and mean nothing by
   * it, so only checking and cards are asked.
   */
  isQuiet(account: LinkedAccount): boolean {
    if (account.closed_on || !account.newest_transaction) return false;
    if (account.group !== 'checking' && account.group !== 'credit') return false;
    return daysSince(account.newest_transaction) > QUIET_DAYS;
  }

  /** The oldest transaction across the bank — how far back Plaid reached. */
  since(item: LinkedItem): string | null {
    const dates = item.accounts
      .map((a) => a.oldest_transaction)
      .filter((d): d is string => !!d)
      .sort();
    return dates[0] ?? null;
  }

  groupName(group: AccountGroup): string {
    return GROUP_NAMES[group];
  }

  /** "Oct 30", read as a local calendar day. */
  day(iso: string): string {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
    });
  }

  ago(iso: string): string {
    const n = daysSince(iso);
    if (n <= 0) return 'today';
    if (n === 1) return 'yesterday';
    return `${n} days ago`;
  }

  hasInvestments(item: LinkedItem): boolean {
    return item.products.includes('investments');
  }

  /** A sync is an instant, not a calendar day: "today 2:44 PM", "Oct 30". */
  when(iso: string | null): string {
    if (!iso) return 'never';
    const at = new Date(iso);
    const time = at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    return at.toDateString() === new Date().toDateString()
      ? `today ${time}`
      : at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ` ${time}`;
  }

  private describe(err: unknown): string {
    const detail = (err as { error?: { detail?: string } })?.error?.detail;
    return detail ?? 'Something went wrong talking to the bank.';
  }
}

const QUIET_DAYS = 21;

const GROUP_NAMES: Record<AccountGroup, string> = {
  checking: 'checking',
  savings: 'savings',
  credit: 'card',
  loan: 'loan',
  retirement: 'retirement',
  investment: 'investment',
  other: 'other',
};

/** Whole days from a local calendar day to today. */
function daysSince(iso: string): number {
  const [y, m, d] = iso.split('-').map(Number);
  const then = new Date(y, m - 1, d);
  const now = new Date();
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((midnight.getTime() - then.getTime()) / 86_400_000);
}

function today(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}
