import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import {
  Category,
  FixedCost,
  MONTH_NAMES,
  PaycheckLine,
  Period,
  ReconcileRow,
  formatMoney,
} from '../models';

/**
 * The config sheets, made editable.
 *
 * `Monthly Fixed Costs`, `Paycheck` and `Rent` were imported and then
 * unreachable, so changing what rent costs still meant opening Excel.
 */
@Component({
  selector: 'app-settings',
  imports: [FormsModule],
  templateUrl: './settings.html',
  styleUrl: './settings.scss',
})
export class Settings {
  private api = inject(Api);

  costs = signal<FixedCost[]>([]);
  paycheck = signal<PaycheckLine[]>([]);
  categories = signal<Category[]>([]);
  periods = signal<Period[]>([]);
  reconcileRows = signal<ReconcileRow[]>([]);
  reconcilePeriod = signal('');
  loading = signal(true);
  busy = signal(false);
  error = signal<string | null>(null);
  notice = signal<string | null>(null);
  expanded = signal<Set<number>>(new Set());

  editingCost = signal<number | null>(null);
  draftAmount = signal('');

  readonly fmt = formatMoney;
  readonly monthNames = MONTH_NAMES;

  monthlyTotal = computed(() =>
    this.costs()
      .reduce((sum, c) => sum + Number(c.amount), 0)
      .toFixed(2),
  );

  /** Per-paycheck gross, minus insurance, tax and savings. */
  takeHome = computed(() => {
    const by = (kind: string) =>
      this.paycheck()
        .filter((l) => l.kind === kind)
        .reduce((s, l) => s + Number(l.amount), 0);
    return (by('INCOME') - by('INSURANCE') - by('TAX') - by('SAVINGS')).toFixed(
      2,
    );
  });

  paycheckGroups = computed(() =>
    (['INCOME', 'INSURANCE', 'TAX', 'SAVINGS'] as const).map((kind) => ({
      kind,
      lines: this.paycheck().filter((l) => l.kind === kind),
      total: this.paycheck()
        .filter((l) => l.kind === kind)
        .reduce((s, l) => s + Number(l.amount), 0)
        .toFixed(2),
    })),
  );

  matched = computed(() =>
    this.reconcileRows().filter((r) => r.actual !== null && r.drift === '0.00'),
  );
  drifted = computed(() =>
    this.reconcileRows().filter((r) => r.drift !== null && r.drift !== '0.00'),
  );
  unlinked = computed(() =>
    this.reconcileRows().filter((r) => r.actual === null),
  );

  constructor() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.api.categories().subscribe((c) => this.categories.set(c));
    this.api.fixedCosts().subscribe({
      next: (c) => {
        this.costs.set(c);
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.loading.set(false);
      },
    });
    this.api.paycheck().subscribe((p) => this.paycheck.set(p));
    this.api.periods().subscribe((p) => {
      this.periods.set(p);
      if (p.length && !this.reconcilePeriod()) {
        // Default to the newest month that actually has spending in it.
        const withData = p.find((x) => x.transaction_count > 5) ?? p[0];
        this.reconcilePeriod.set(`${withData.year}-${withData.month}`);
        this.loadReconcile();
      }
    });
  }

  loadReconcile(): void {
    const [year, month] = this.reconcilePeriod().split('-').map(Number);
    if (!year || !month) return;
    this.api.reconcile(year, month).subscribe({
      next: (r) => this.reconcileRows.set(r.rows),
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  onReconcilePeriod(value: string): void {
    this.reconcilePeriod.set(value);
    this.loadReconcile();
  }

  periodLabel(p: Period): string {
    return `${MONTH_NAMES[p.month - 1]} ${p.year}`;
  }

  toggle(id: number): void {
    this.expanded.update((set) => {
      const next = new Set(set);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  startEdit(c: FixedCost): void {
    this.editingCost.set(c.id);
    this.draftAmount.set(c.amount);
  }

  cancelEdit(): void {
    this.editingCost.set(null);
    this.draftAmount.set('');
  }

  saveAmount(c: FixedCost): void {
    const amount = this.draftAmount().trim();
    if (!amount || amount === c.amount) {
      this.cancelEdit();
      return;
    }
    this.busy.set(true);
    this.api.updateFixedCost(c.id, { amount }).subscribe({
      next: () => {
        this.notice.set(
          `${c.description} is now ${this.fmt(amount)}. The old figure is kept, ` +
            'so last month still reconciles against what it actually was.',
        );
        this.busy.set(false);
        this.cancelEdit();
        this.load();
        this.loadReconcile();
        setTimeout(() => this.notice.set(null), 6000);
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.busy.set(false);
      },
    });
  }

  /** Point a commitment at whoever actually charges for it. */
  link(row: ReconcileRow, merchantId: number, name: string): void {
    this.busy.set(true);
    this.api
      .updateFixedCost(row.fixed_cost_id, { merchant_id: merchantId })
      .subscribe({
        next: () => {
          this.notice.set(`${row.description} now reconciles against ${name}.`);
          this.busy.set(false);
          this.loadReconcile();
          setTimeout(() => this.notice.set(null), 5000);
        },
        error: (e) => {
          this.error.set(this.describe(e));
          this.busy.set(false);
        },
      });
  }

  private describe(e: unknown): string {
    const err = e as { status?: number; error?: { detail?: string } };
    if (err?.status === 0)
      return 'Cannot reach the API. Is the backend running?';
    return err?.error?.detail ?? 'Something went wrong.';
  }
}
