import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import {
  AccountRow,
  HoldingRow,
  Portfolio,
  PositionsCommitResult,
  PositionsPreview,
  PreviewAccount,
  formatMoney,
  parseDay,
} from '../models';

/** A statement account plus the answer given to "which account is this?". */
interface Mapping {
  entry: PreviewAccount;
  accountId: number | null;
}

/**
 * What the portfolio is worth, and how much of that was ever saved.
 *
 * Two numbers do the work here. `value` is what the positions are worth at
 * today's prices; `cost_basis` is what was paid for them. The difference is the
 * part of the balance the market provided rather than the budget — and on this
 * data that is 41% of the retirement account, which no amount of budgeting
 * produced and no other screen could show.
 *
 * Every current value is labelled with how it was reached. A live quote, a
 * money market at par, a stand-in's movement, or the statement figure carried
 * unchanged are four quite different claims, and collapsing them into one
 * confident total is the failure this screen exists to avoid.
 */
@Component({
  selector: 'app-investments',
  imports: [FormsModule],
  templateUrl: './investments.html',
  styleUrl: './investments.scss',
})
export class Investments {
  private api = inject(Api);

  portfolio = signal<Portfolio | null>(null);
  accounts = signal<AccountRow[]>([]);
  loading = signal(true);
  busy = signal(false);
  error = signal<string | null>(null);

  // Importing a statement.
  importOpen = signal(false);
  fileName = signal<string | null>(null);
  preview = signal<PositionsPreview | null>(null);
  mappings = signal<Mapping[]>([]);
  recordBalances = signal(true);
  result = signal<PositionsCommitResult | null>(null);
  institution = signal('Fidelity');

  // Setting a stand-in for something with no public quote.
  proxyDraft = signal<Partial<Record<string, string>>>({});
  proxyBusy = signal<string | null>(null);
  proxyError = signal<string | null>(null);

  readonly fmt = formatMoney;

  constructor() {
    this.load();
    this.api
      .accounts()
      .subscribe((a) => this.accounts.set(a.filter((x) => !x.closed_on)));
  }

  load(refresh = true): void {
    this.loading.set(true);
    this.api.portfolio(refresh).subscribe({
      next: (p) => {
        this.portfolio.set(p);
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.loading.set(false);
      },
    });
  }

  /** The oldest statement behind any of these figures. */
  oldestStatement = computed(() => {
    const dates = (this.portfolio()?.accounts ?? []).map((a) => a.as_of).sort();
    return dates[0] ?? null;
  });

  /**
   * Growth as a share of what was paid in, ready to display.
   *
   * A ratio for a label, not money — the one arithmetic this app does in the
   * browser. Formatted here rather than with DecimalPipe so the template does
   * not have to import CommonModule for a single figure.
   */
  growthLabel = computed(() => {
    const p = this.portfolio();
    if (!p?.cost_basis || p.gain === null) return null;
    const basis = Number(p.cost_basis);
    if (!basis) return null;
    const percent = (Number(p.gain) / basis) * 100;
    return `${percent > 0 ? '+' : ''}${percent.toFixed(1)}% on what was paid`;
  });

  /** Movement since the statements, which is the estimate's whole content. */
  sinceStatement = computed(() => {
    const p = this.portfolio();
    if (!p) return null;
    return Number(p.value) - Number(p.statement_value);
  });

  hasEstimate = computed(() =>
    (this.portfolio()?.accounts ?? []).some((a) => a.is_estimated),
  );

  // --- Presentation --------------------------------------------------------

  longDate(iso: string | null): string {
    if (!iso) return '—';
    return parseDay(iso).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    });
  }

  pricedAt(iso: string | null): string {
    if (!iso) return 'not yet priced';
    const when = new Date(iso);
    const today = new Date().toDateString() === when.toDateString();
    const time = when.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
    });
    return today
      ? `prices at ${time}`
      : `prices from ${when.toLocaleDateString(undefined, {
          month: 'short',
          day: 'numeric',
        })}, ${time}`;
  }

  /** What each pricing basis claims, in words rather than a code. */
  basisLabel(basis: HoldingRow['basis']): string {
    return {
      LIVE: 'live',
      PAR: 'at par',
      PROXY: 'estimated',
      CARRIED: 'from statement',
    }[basis];
  }

  basisTitle(row: HoldingRow): string {
    switch (row.basis) {
      case 'LIVE':
        return `${row.quantity} units at a live quote${
          row.priced_on ? `, priced ${row.priced_on}` : ''
        }`;
      case 'PAR':
        return 'A money market is a dollar a unit, so this figure does not move';
      case 'PROXY':
        return `No public quote. Moved by the change in ${row.proxy_symbol} since the statement — its movement only, never its price`;
      default:
        return row.note ?? 'The statement figure, carried unchanged';
    }
  }

  quantityOf(row: HoldingRow): string {
    return row.quantity ?? '—';
  }

  signed(value: string | number | null): string {
    if (value === null) return '—';
    const n = typeof value === 'number' ? value : Number(value);
    return `${n > 0 ? '+' : ''}${formatMoney(n.toFixed(2))}`;
  }

  // --- Importing a statement ----------------------------------------------

  onFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    this.fileName.set(file.name);
    this.busy.set(true);
    this.error.set(null);
    this.result.set(null);
    this.api.previewPositions(file, this.institution()).subscribe({
      next: (p) => {
        this.preview.set(p);
        this.mappings.set(
          p.accounts.map((entry) => ({ entry, accountId: entry.account_id })),
        );
        this.busy.set(false);
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.busy.set(false);
      },
    });
    input.value = '';
  }

  setMapping(mapping: Mapping, value: string): void {
    this.mappings.update((all) =>
      all.map((m) =>
        m.entry.external_number === mapping.entry.external_number
          ? { ...m, accountId: value ? Number(value) : null }
          : m,
      ),
    );
  }

  /** Every statement account needs an answer before anything can be written. */
  unmappedCount = computed(
    () => this.mappings().filter((m) => m.accountId === null).length,
  );

  canCommit = computed(
    () =>
      !!this.preview()?.as_of &&
      this.mappings().length > 0 &&
      this.unmappedCount() === 0,
  );

  /** Two statement accounts pointed at one budgeter account would overwrite
   *  each other's positions. Caught here rather than after the write. */
  duplicateMapping = computed(() => {
    const chosen = this.mappings()
      .map((m) => m.accountId)
      .filter((id): id is number => id !== null);
    return chosen.length !== new Set(chosen).size;
  });

  commit(): void {
    const preview = this.preview();
    if (!preview?.as_of || !this.canCommit() || this.duplicateMapping()) return;

    this.busy.set(true);
    this.error.set(null);
    this.api
      .commitPositions({
        as_of: preview.as_of,
        institution: preview.institution,
        record_balances: this.recordBalances(),
        accounts: this.mappings().map((m) => ({
          external_number: m.entry.external_number,
          external_name: m.entry.external_name,
          account_id: m.accountId,
          positions: m.entry.positions.map((p) => ({
            symbol: p.symbol,
            description: p.description,
            quantity: p.quantity,
            price: p.price,
            value: p.value,
            cost_basis: p.cost_basis,
            kind: p.kind,
          })),
        })),
      })
      .subscribe({
        next: (r) => {
          this.result.set(r);
          this.preview.set(null);
          this.mappings.set([]);
          this.fileName.set(null);
          this.busy.set(false);
          this.importOpen.set(false);
          this.load();
        },
        error: (e) => {
          this.error.set(this.describe(e));
          this.busy.set(false);
        },
      });
  }

  cancelPreview(): void {
    this.preview.set(null);
    this.mappings.set([]);
    this.fileName.set(null);
  }

  // --- Stand-ins -----------------------------------------------------------

  setProxyDraft(symbol: string, value: string): void {
    this.proxyDraft.update((d) => ({ ...d, [symbol]: value }));
  }

  saveProxy(symbol: string): void {
    const value = (this.proxyDraft()[symbol] ?? '').trim();
    if (!value) return;
    this.proxyBusy.set(symbol);
    this.proxyError.set(null);
    this.api.setQuoteSymbol(symbol, value).subscribe({
      next: () => {
        this.proxyBusy.set(null);
        this.proxyDraft.update((d) => ({ ...d, [symbol]: '' }));
        this.load();
      },
      error: (e) => {
        this.proxyError.set(this.describe(e));
        this.proxyBusy.set(null);
      },
    });
  }

  private describe(error: unknown): string {
    const detail = (error as { error?: { detail?: string } })?.error?.detail;
    return detail ?? 'Something went wrong. Check the API is running.';
  }
}
