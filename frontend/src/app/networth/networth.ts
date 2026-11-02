import { Component, computed, effect, inject, signal, untracked } from '@angular/core';

import { Api } from '../api';
import { AccountRow, NetWorth, NetWorthPoint, Portfolio } from '../models';
import { Sync } from '../sync';

/**
 * What you own, what you owe, and how that has moved.
 *
 * The chart is drawn as inline SVG from the series the server sends. No chart
 * library: one path, one baseline and a handful of ticks is less code than
 * configuring one, and it cannot drift from the numbers in the table below it.
 *
 * Every point carries how many accounts reported that day, because without it
 * a day when two of eleven accounts were read looks exactly like a day net
 * worth collapsed. Points below the full count are drawn hollow and named as
 * partial rather than quietly plotted alongside the rest.
 */
@Component({
  selector: 'app-networth',
  imports: [],
  templateUrl: './networth.html',
  styleUrl: './networth.scss',
})
export class Networth {
  private api = inject(Api);
  private sync = inject(Sync);

  data = signal<NetWorth | null>(null);
  accounts = signal<AccountRow[]>([]);
  portfolio = signal<Portfolio | null>(null);
  loading = signal(true);

  readonly width = 760;
  readonly height = 180;

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

  partialCount = computed(() => {
    const d = this.data();
    return d ? d.points.length - this.complete().length : 0;
  });

  path = computed(() => {
    const points = this.complete();
    if (points.length < 2) return '';
    const values = points.map((p) => Number(p.net));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    return points
      .map((p, i) => {
        const x = (i / (points.length - 1)) * this.width;
        const y = this.height - ((Number(p.net) - min) / span) * (this.height - 12) - 6;
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  });

  liabilities = computed(() =>
    this.accounts().filter((a) => a.is_liability),
  );

  assets = computed(() => this.accounts().filter((a) => !a.is_liability));

  constructor() {
    this.load();
    // A balance is only written by a refresh, so the series grows by one day
    // for every day the app is opened — the header's daily refresh does that.
    effect(() => {
      if (this.sync.version()) untracked(() => this.load());
    });
  }

  private load(): void {
    this.api.netWorth().subscribe({
      next: (d) => {
        this.data.set(d);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
    this.api.accounts().subscribe((a) => this.accounts.set(a));
    this.api.portfolio().subscribe((p) => this.portfolio.set(p));
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
