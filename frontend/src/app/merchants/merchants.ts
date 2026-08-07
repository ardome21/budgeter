import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import { Merchant } from '../models';

/**
 * The consolidation pass.
 *
 * Normalization is deliberately conservative, so it under-merges — wrongly
 * splitting one shop into two records is a click to fix, wrongly merging two
 * shops silently corrupts every total they appear in. This screen is where a
 * human resolves the leftovers, and it suggests likely pairs rather than
 * acting on them.
 */
@Component({
  selector: 'app-merchants',
  imports: [FormsModule],
  templateUrl: './merchants.html',
  styleUrl: './merchants.scss',
})
export class Merchants {
  private api = inject(Api);

  rows = signal<Merchant[]>([]);
  search = signal('');
  loading = signal(true);
  error = signal<string | null>(null);
  selected = signal<Set<number>>(new Set());

  /**
   * Names sharing a first word are the usual split: 'Rhino Market',
   * 'Rhino Mart', 'Rhino Market Deli'. A suggestion, never an action.
   */
  suggestions = computed(() => {
    const groups = new Map<string, Merchant[]>();
    for (const m of this.rows()) {
      const head = m.canonical_name.split(' ')[0].toLowerCase();
      if (head.length < 4) continue;
      groups.set(head, [...(groups.get(head) ?? []), m]);
    }
    return (
      [...groups.values()]
        .filter((g) => g.length > 1)
        .sort((a, b) => this.uses(b) - this.uses(a))
        // Capped: this is a review queue, not a report. Working the top of the
        // list is what shrinks the rest.
        .slice(0, 12)
    );
  });

  showAll = signal(false);

  /** The long tail is one-transaction merchants; 40 is enough to scan. */
  visible = computed(() =>
    this.showAll() ? this.rows() : this.rows().slice(0, 40),
  );

  constructor() {
    this.reload();
  }

  reload(): void {
    this.loading.set(true);
    this.api.merchants(this.search() || undefined).subscribe({
      next: (rows) => {
        this.rows.set(rows);
        this.selected.set(new Set());
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.loading.set(false);
      },
    });
  }

  onSearch(value: string): void {
    this.search.set(value);
    this.reload();
  }

  uses(group: Merchant[]): number {
    return group.reduce((n, m) => n + m.transaction_count, 0);
  }

  toggle(id: number): void {
    this.selected.update((set) => {
      const next = new Set(set);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  /** Merge every other member of the group into `keep`. */
  mergeGroup(group: Merchant[], keep: Merchant): void {
    const others = group.filter((m) => m.id !== keep.id);
    if (!others.length) return;
    if (
      !confirm(
        `Merge ${others.length} merchant(s) into "${keep.canonical_name}"?\n\n` +
          others.map((o) => `  • ${o.canonical_name}`).join('\n') +
          '\n\nTransactions move across. This cannot be undone automatically.',
      )
    ) {
      return;
    }
    let remaining = others.length;
    for (const other of others) {
      this.api.mergeMerchant(other.id, keep.id).subscribe({
        next: () => {
          if (--remaining === 0) this.reload();
        },
        error: (e) => {
          this.error.set(this.describe(e));
          if (--remaining === 0) this.reload();
        },
      });
    }
  }

  private describe(e: unknown): string {
    const err = e as { status?: number; error?: { detail?: string } };
    if (err?.status === 0)
      return 'Cannot reach the API. Is the backend running?';
    return err?.error?.detail ?? 'Something went wrong.';
  }
}
