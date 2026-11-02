import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Api } from '../api';
import { Overview as OverviewData, PayBreakdown, PayStub, StandingBucket } from '../models';
import { Sync } from '../sync';
import { UsdPipe } from '../usd';
import { PayStubForm } from './pay-stub';

/**
 * Where you stand, and how money moves through a month.
 *
 * The top is today: what you have by kind of money, and how long the cash
 * would last. The middle is a typical month — the last three finished ones —
 * as earned, spent and kept. The table is each month, newest first.
 *
 * The bank only sees take-home pay, so what a paycheck puts away before it
 * lands (a 401(k), an HSA) comes from the pay stub, typed once. Until then the
 * savings rate counts only what is left in the bank, and the page says so.
 */
@Component({
  selector: 'app-overview',
  imports: [RouterLink, UsdPipe, PayStubForm],
  templateUrl: './overview.html',
  styleUrl: './overview.scss',
})
export class Overview {
  private api = inject(Api);
  private sync = inject(Sync);

  data = signal<OverviewData | null>(null);
  stubs = signal<PayStub[]>([]);
  editing = signal(false);
  error = signal<string | null>(null);

  readonly buckets: { key: StandingBucket; label: string; note: string }[] = [
    { key: 'cash', label: 'Cash', note: 'checking and savings' },
    { key: 'invested', label: 'Invested', note: 'brokerage, HSA, stock plan' },
    { key: 'retirement', label: 'Retirement', note: 'not reachable until later' },
    { key: 'owed', label: 'Owed', note: 'card balances' },
  ];

  /** What each group of stub lines is called in the breakdown. */
  readonly groupLabels: Record<string, string> = {
    TAX: 'Taxes',
    SAVING: 'You saved for retirement and health',
    DEDUCTION: 'Insurance and other deductions',
    EMPLOYER: 'Your employer added',
  };

  /** The groups that come out of gross, in order — not the employer's. */
  outOfPay(b: PayBreakdown) {
    return b.groups.filter((g) => g.kind !== 'EMPLOYER');
  }

  employer(b: PayBreakdown) {
    return b.groups.find((g) => g.kind === 'EMPLOYER') ?? null;
  }

  /** Any month in the table has stub figures, so the stub columns are worth showing. */
  anyStub = computed(() => this.data()?.flow.some((f) => f.has_stub) ?? false);

  constructor() {
    this.load();
    effect(() => {
      if (this.sync.version()) untracked(() => this.load());
    });
  }

  bucketTotal(d: OverviewData, key: StandingBucket): string {
    return d[key];
  }

  accountsIn(d: OverviewData, key: StandingBucket) {
    return d.accounts.filter((a) => a.bucket === key);
  }

  /** "0.183" → "18%". Read from the string; one decimal only under 10%. */
  percent(rate: string | null): string {
    if (rate === null) return '—';
    const tenths = Math.round(Number(rate) * 1000);
    const abs = Math.abs(tenths);
    const sign = tenths < 0 ? '−' : '';
    return abs < 100 ? `${sign}${(abs / 10).toFixed(1)}%` : `${sign}${Math.round(abs / 10)}%`;
  }

  negative(value: string | null): boolean {
    return value?.startsWith('-') ?? false;
  }

  /** "October 2026" from "2026-10-01". Parsed as local, never `new Date(iso)`. */
  label(iso: string, short = false): string {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d || 1).toLocaleDateString(
      undefined,
      short ? { month: 'short', year: 'numeric' } : { month: 'long', year: 'numeric' },
    );
  }

  day(iso: string): string {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  /** "Aug–Oct 2026", the months the typical figures are averaged over. */
  span(months: string[]): string {
    if (!months.length) return '';
    const oldest = months[months.length - 1];
    const [y, m] = oldest.split('-').map(Number);
    const from = new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'short' });
    return months.length === 1 ? this.label(months[0], true) : `${from}–${this.label(months[0], true)}`;
  }

  stubSaved(): void {
    this.editing.set(false);
    this.load();
  }

  private load(): void {
    this.error.set(null);
    this.api.overview().subscribe({
      next: (d) => this.data.set(d),
      error: () => this.error.set('Could not load the overview.'),
    });
    this.api.payStubs().subscribe((s) => this.stubs.set(s));
  }
}
