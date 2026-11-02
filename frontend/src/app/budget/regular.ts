import { Component, computed, effect, inject, input, output, signal, untracked } from '@angular/core';

import { Api } from '../api';
import { Category, RegularCharge } from '../models';
import { UsdPipe } from '../usd';

interface Merchant {
  merchant: string;
  category_id: number;
  ruled: boolean;
  charges: { amount: string; day: number }[];
}

/**
 * The charges that come every month, and the one place a category choice
 * reaches future rows: pick Subscriptions for Netflix here and every Netflix
 * charge, past and to come, is filed there. Rows you filed somewhere else by
 * hand stay where you put them.
 */
@Component({
  selector: 'app-regular',
  imports: [UsdPipe],
  template: `
    @if (merchants().length) {
      <h2>Regular charges</h2>
      <p class="dim small">
        Charged in each of the last three months. Choosing a category here files
        every charge from that merchant there — the ones already in, and the
        ones to come. A charge you filed somewhere else by hand stays put.
      </p>
      @if (message()) {
        <p class="banner" [class.error]="failed()">{{ message() }}</p>
      }
      <div class="card scroll-x">
        <table>
          <thead>
            <tr>
              <th>Merchant</th>
              <th>Charged</th>
              <th>Category</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            @for (m of merchants(); track m.merchant) {
              <tr>
                <td>{{ m.merchant }}</td>
                <td class="dim">
                  @for (c of m.charges; track $index) {
                    <span class="charge">{{ c.amount | usd }} on the {{ ordinal(c.day) }}</span>
                  }
                </td>
                <td>
                  <select (change)="file(m, +$any($event.target).value)"
                          [disabled]="saving() === m.merchant"
                          [attr.aria-label]="'Category for ' + m.merchant">
                    @for (c of categories(); track c.id) {
                      <option [value]="c.id" [selected]="c.id === m.category_id">{{ c.name }}</option>
                    }
                  </select>
                </td>
                <td class="actions">
                  @if (m.ruled) {
                    <button type="button" class="link-button" (click)="file(m, null)"
                            title="Go back to filing it by the bank's label">
                      Use the bank's
                    </button>
                  }
                </td>
              </tr>
            }
          </tbody>
        </table>
      </div>
    }
  `,
  styles: `
    h2 { font-size: 15px; font-weight: 600; margin: 2rem 0 0.2rem; }
    .small { font-size: 13px; max-width: 44rem; }
    .charge { white-space: nowrap; & + .charge::before { content: ' · '; } }
    .actions { width: 1%; text-align: right; white-space: nowrap; }
    .link-button {
      all: unset; cursor: pointer; font-size: 13px; color: var(--text-dim);
      border-bottom: 1px solid var(--border);
      &:hover { color: var(--text); border-bottom-color: var(--accent); }
      &:focus-visible { outline: 2px solid var(--accent-dim); }
    }
  `,
})
export class Regular {
  private api = inject(Api);

  /** Bumped by the page when categories change, so the pickers follow. */
  version = input(0);
  /** A rule moved rows, so the figures above are stale. */
  changed = output<void>();

  private rows = signal<RegularCharge[]>([]);
  categories = signal<Category[]>([]);
  saving = signal<string | null>(null);
  message = signal<string | null>(null);
  failed = signal(false);

  /** One row per merchant: Apple bills two subscriptions, one rule files both. */
  merchants = computed(() => {
    const out = new Map<string, Merchant>();
    for (const r of this.rows()) {
      const m = out.get(r.merchant) ?? {
        merchant: r.merchant,
        category_id: r.category_id,
        ruled: r.ruled,
        charges: [],
      };
      m.charges.push({ amount: r.amount, day: r.day });
      out.set(r.merchant, m);
    }
    return [...out.values()];
  });

  constructor() {
    effect(() => {
      this.version();
      untracked(() => this.load());
    });
  }

  file(m: Merchant, categoryId: number | null): void {
    this.saving.set(m.merchant);
    this.message.set(null);
    this.api.setMerchantRule(m.merchant, categoryId).subscribe({
      next: ({ moved }) => {
        this.saving.set(null);
        this.failed.set(false);
        const where = categoryId === null
          ? "back to the bank's label"
          : `under ${this.categories().find((c) => c.id === categoryId)?.name}`;
        this.message.set(
          `${m.merchant} is filed ${where}` +
            (moved ? ` — ${moved} charge${moved === 1 ? '' : 's'} moved.` : '.'),
        );
        this.load();
        this.changed.emit();
      },
      error: (e) => {
        this.saving.set(null);
        this.failed.set(true);
        this.message.set(e?.error?.detail ?? 'That did not save.');
        this.load();
      },
    });
  }

  ordinal(n: number): string {
    const tens = n % 100;
    if (tens >= 11 && tens <= 13) return `${n}th`;
    return n + ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] ?? 'th');
  }

  private load(): void {
    this.api.regular().subscribe((rows) => this.rows.set(rows));
    this.api.categories().subscribe((c) => this.categories.set(c));
  }
}
