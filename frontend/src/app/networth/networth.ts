import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';

import { Api } from '../api';
import { AccountGroup, AccountRow, NetWorth, NetWorthPoint, Portfolio } from '../models';
import { UsdPipe } from '../usd';
import { Sync } from '../sync';
import { NetWorthChart } from './chart';

/**
 * What you own, what you owe, and how that has moved.
 *
 * The chart is its own component (`chart.ts`): hover any day to see what it
 * is made of.
 *
 * Every point carries how many accounts reported that day, because without it
 * a day when two of eleven accounts were read looks exactly like a day net
 * worth collapsed. Points below the full count are drawn hollow and named as
 * partial rather than quietly plotted alongside the rest.
 *
 * Everything on the page can be narrowed to a selection of accounts — card
 * debt alone, or every investment — by group or one account at a time. The
 * figures for a selection are summed by the server, never here: the browser
 * has no decimal type to add money with. The selection lives in the URL, so a
 * view can be bookmarked, and is remembered for the next visit.
 */
@Component({
  selector: 'app-networth',
  imports: [NetWorthChart, UsdPipe],
  templateUrl: './networth.html',
  styleUrl: './networth.scss',
})
export class Networth {
  private api = inject(Api);
  private sync = inject(Sync);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  /** In display order. A group with no accounts in it is not shown. */
  readonly groupOrder: { key: AccountGroup; label: string }[] = [
    { key: 'checking', label: 'Checking' },
    { key: 'savings', label: 'Savings' },
    { key: 'credit', label: 'Credit cards' },
    { key: 'loan', label: 'Loans' },
    { key: 'retirement', label: 'Retirement' },
    { key: 'investment', label: 'Other investments' },
    { key: 'other', label: 'Other' },
  ];

  /** Ids of the accounts being looked at. Every account, until narrowed. */
  selected = signal<ReadonlySet<number>>(new Set());

  data = signal<NetWorth | null>(null);
  accounts = signal<AccountRow[]>([]);
  portfolio = signal<Portfolio | null>(null);
  loading = signal(true);

  /**
   * Only the days that reported as fully as any day ever has.
   *
   * Compared against the best reading actually seen, not against the number of
   * open accounts. Those differ whenever an account exists that Plaid never
   * returns a balance for — no Balance row is written at all for it, so
   * `accounts_reporting` could never reach `account_count` and the filter
   * dropped every point, blanking the chart permanently with nothing on screen
   * to explain why. The purpose of the filter is to keep a partial day from
   * reading as a collapse, and the high-water mark does that exactly as well.
   */
  best = computed(() => {
    const points = this.data()?.points ?? [];
    return points.reduce((most, p) => Math.max(most, p.accounts_reporting), 0);
  });

  complete = computed(() => {
    const points = this.data()?.points ?? [];
    const best = this.best();
    return points.filter((p) => p.accounts_reporting >= best);
  });

  /** Accounts that have never reported a balance. Named on screen rather than
   *  silently excluded — an account contributing nothing to net worth is a
   *  fact about the total, not a detail. */
  silent = computed(() => {
    const d = this.data();
    if (!d) return 0;
    return Math.max(0, d.account_count - this.best());
  });

  /** How many plotted days lean on a rebuilt figure — said under the chart
   *  so an estimate is never read as a measurement. */
  estimated = computed(() => this.complete().filter((p) => p.estimated).length);

  partialCount = computed(() => {
    const d = this.data();
    return d ? d.points.length - this.complete().length : 0;
  });

  groups = computed(() =>
    this.groupOrder
      .map((g) => ({ ...g, accounts: this.accounts().filter((a) => a.group === g.key) }))
      .filter((g) => g.accounts.length),
  );

  everything = computed(() => this.selected().size === this.accounts().length);

  /** Whether a group's chip is on, off, or partly on. */
  groupState(accounts: AccountRow[]): 'on' | 'some' | 'off' {
    const picked = accounts.filter((a) => this.selected().has(a.id)).length;
    return picked === accounts.length ? 'on' : picked ? 'some' : 'off';
  }

  constructor() {
    this.api.accounts().subscribe((accounts) => {
      this.accounts.set(accounts);
      this.selected.set(this.restore(accounts));
      this.load();
    });
    // A balance is only written by a refresh, so the series grows by one day
    // for every day the app is opened — the header's daily refresh does that.
    effect(() => {
      if (this.sync.version()) {
        untracked(() => {
          this.api.accounts().subscribe((a) => this.accounts.set(a));
          this.load();
        });
      }
    });
  }

  toggleGroup(accounts: AccountRow[]): void {
    const next = new Set(this.selected());
    const on = this.groupState(accounts) === 'on';
    for (const a of accounts) {
      if (on) next.delete(a.id);
      else next.add(a.id);
    }
    this.choose(next);
  }

  toggleAccount(id: number): void {
    const next = new Set(this.selected());
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this.choose(next);
  }

  all(): void {
    this.choose(new Set(this.accounts().map((a) => a.id)));
  }

  none(): void {
    this.choose(new Set());
  }

  private choose(ids: Set<number>): void {
    this.selected.set(ids);
    const value = this.everything() ? null : [...ids].sort((a, b) => a - b).join(',');
    if (value === null) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, value);
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { accounts: value },
      replaceUrl: true,
    });
    this.load();
  }

  /** The URL first, so a bookmark means what it says; then the last visit;
   *  then everything. Ids for accounts that no longer exist are dropped. */
  private restore(accounts: AccountRow[]): Set<number> {
    const saved =
      this.route.snapshot.queryParamMap.get('accounts') ?? localStorage.getItem(STORAGE_KEY);
    const known = new Set(accounts.map((a) => a.id));
    if (saved === null) return known;
    return new Set(saved.split(',').map(Number).filter((id) => known.has(id)));
  }

  private load(): void {
    const ids = this.everything() ? undefined : [...this.selected()];
    if (ids?.length === 0) {
      this.data.set(null);
      this.portfolio.set(null);
      this.loading.set(false);
      return;
    }
    this.api.netWorth(365, ids).subscribe({
      next: (d) => {
        this.data.set(d);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
    this.api.portfolio(ids).subscribe((p) => this.portfolio.set(p));
  }

  /** Calendar dates, parsed as local. `new Date(iso)` reads them as UTC
   * midnight, which in this timezone renders as the day before. */
  day(iso: string): string {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  }

  span(): string {
    const points = this.complete();
    if (points.length < 2) return '';
    return `${this.day(points[0].as_of)} — ${this.day(points[points.length - 1].as_of)}`;
  }

  isStale(account: AccountRow): boolean {
    const latest = this.data()?.latest?.as_of;
    return !!latest && !!account.balance_as_of && account.balance_as_of < latest;
  }

  trackPoint(_: number, point: NetWorthPoint): string {
    return point.as_of;
  }
}

const STORAGE_KEY = 'networth.accounts';
