import { Component, computed, inject, input, output, signal } from '@angular/core';

import { Api } from '../api';
import { AccountRow, PayLine, PayLineKind, PayStub } from '../models';
import { fromCents, toCents } from '../money';
import { UsdPipe } from '../usd';

interface Row {
  kind: PayLineKind;
  name: string;
  amount: string;
  account_id: number | null;
}

/** The usual lines on a stub, offered blank. A blank line is not saved. */
const STARTER: Row[] = [
  { kind: 'TAX', name: 'Federal income tax', amount: '', account_id: null },
  { kind: 'TAX', name: 'State income tax', amount: '', account_id: null },
  { kind: 'TAX', name: 'Social Security', amount: '', account_id: null },
  { kind: 'TAX', name: 'Medicare', amount: '', account_id: null },
  { kind: 'SAVING', name: '401(k)', amount: '', account_id: null },
  { kind: 'SAVING', name: 'HSA', amount: '', account_id: null },
  { kind: 'SAVING', name: 'ESPP', amount: '', account_id: null },
  { kind: 'DEDUCTION', name: 'Health insurance', amount: '', account_id: null },
];

/**
 * What a paycheck loses before the bank sees it, typed once from a pay stub.
 *
 * It holds from its month on until another is entered, the way a budget does.
 * The take-home it works out is shown beside what the bank actually received,
 * so a stub typed wrong is visible before it skews every savings figure.
 */
@Component({
  selector: 'app-pay-stub',
  imports: [UsdPipe],
  templateUrl: './pay-stub.html',
  styleUrl: './pay-stub.scss',
})
export class PayStubForm {
  private api = inject(Api);

  /** Every stub on file, newest first. */
  stubs = input.required<PayStub[]>();
  /** The first month there is history for, YYYY-MM-DD: where a first stub starts. */
  historyStart = input<string | null>(null);
  /** What the bank typically received in a month, for the check line. */
  typicalTakeHome = input<string | null>(null);
  /** Retirement accounts a saving line can be paid into. */
  plans = signal<AccountRow[]>([]);
  saved = output<void>();
  closed = output<void>();

  month = signal('');
  periods = signal(24);
  gross = signal('');
  rows = signal<Row[]>([]);
  saving = signal(false);
  error = signal<string | null>(null);

  readonly kinds: { key: PayLineKind; label: string }[] = [
    { key: 'TAX', label: 'Tax' },
    { key: 'SAVING', label: 'You save' },
    { key: 'DEDUCTION', label: 'Other deduction' },
    { key: 'EMPLOYER', label: 'Employer adds (fixed)' },
  ];

  readonly schedules = [
    { periods: 24, label: 'Twice a month' },
    { periods: 26, label: 'Every two weeks' },
    { periods: 12, label: 'Monthly' },
    { periods: 52, label: 'Weekly' },
  ];

  /** Boxes holding something that is not an amount. Save waits on them. */
  invalid = computed(() => {
    const bad = new Set<number>();
    this.rows().forEach((r, i) => {
      if (r.amount.trim() && toCents(r.amount) === null) bad.add(i);
    });
    return bad;
  });

  grossBad = computed(() => toCents(this.gross()) === null);

  /** Gross less every line but the employer's, in whole cents as it is typed. */
  takeHome = computed(() => {
    const gross = toCents(this.gross());
    if (gross === null) return null;
    let out = 0n;
    for (const r of this.rows()) {
      const cents = toCents(r.amount);
      if (cents !== null && r.kind !== 'EMPLOYER') out += cents;
    }
    return fromCents(gross - out);
  });

  /** What the bank received per paycheck, on average. Includes interest, so "about". */
  receivedPerCheck = computed(() => {
    const monthly = toCents(this.typicalTakeHome() ?? '');
    if (monthly === null) return null;
    return fromCents((monthly * 12n) / BigInt(this.periods()));
  });

  ngOnInit(): void {
    this.api
      .accounts()
      .subscribe((all) => this.plans.set(all.filter((a) => a.is_retirement)));
    const current = this.stubs()[0];
    if (current) {
      this.month.set(current.effective_from.slice(0, 7));
      this.periods.set(current.periods_per_year);
      this.gross.set(current.gross);
      this.rows.set(current.lines.map((l) => ({ ...l })));
    } else {
      this.month.set((this.historyStart() ?? today()).slice(0, 7));
      this.rows.set(STARTER.map((r) => ({ ...r })));
    }
  }

  update(i: number, patch: Partial<Row>): void {
    this.rows.update((rows) => rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }

  add(): void {
    this.rows.update((rows) => [...rows, { kind: 'TAX', name: '', amount: '', account_id: null }]);
  }

  remove(i: number): void {
    this.rows.update((rows) => rows.filter((_, j) => j !== i));
  }

  save(): void {
    if (this.invalid().size || this.grossBad() || !this.month()) return;
    const lines: PayLine[] = this.rows()
      .filter((r) => r.amount.trim() !== '' && r.name.trim() !== '')
      .map((r) => ({
        kind: r.kind,
        name: r.name.trim(),
        amount: fromCents(toCents(r.amount)!),
        account_id: r.kind === 'SAVING' ? r.account_id : null,
      }));
    this.saving.set(true);
    this.error.set(null);
    this.api
      .setPayStub(this.month(), {
        periods_per_year: this.periods(),
        gross: fromCents(toCents(this.gross())!),
        lines,
      })
      .subscribe({
        next: () => {
          this.saving.set(false);
          this.saved.emit();
        },
        error: (e) => {
          this.saving.set(false);
          this.error.set(e?.error?.detail ?? 'That did not save.');
        },
      });
  }

  removeStub(stub: PayStub): void {
    this.api.deletePayStub(stub.effective_from).subscribe(() => this.saved.emit());
  }

  /** "June 2026" from "2026-06-01". Parsed as local, never `new Date(iso)`. */
  label(iso: string): string {
    const [y, m] = iso.split('-').map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  }
}

function today(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-01`;
}
