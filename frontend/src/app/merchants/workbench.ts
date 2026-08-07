import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { Api } from '../api';
import { MerchantRow, MerchantSort, formatMoney } from '../models';

/**
 * The merchant workbench.
 *
 * Sorted by spend, because 277 of the 401 merchants were seen exactly once and
 * an alphabetical list buries the handful that matter.
 *
 * The hand-merge here exists because the review queue structurally cannot
 * propose everything: its rule keys on the first word, so 'Airbnb',
 * 'Future Rent Airbnb' and 'Revolution Park Air Bnb' — $6,599 across three
 * records — will never be suggested. They have to be picked.
 */
@Component({
  selector: 'app-workbench',
  imports: [FormsModule, RouterLink],
  templateUrl: './workbench.html',
  styleUrl: './workbench.scss',
})
export class Workbench {
  private api = inject(Api);

  rows = signal<MerchantRow[]>([]);
  total = signal(0);
  search = signal('');
  sort = signal<MerchantSort>('spend');
  loading = signal(true);
  busy = signal(false);
  error = signal<string | null>(null);
  notice = signal<string | null>(null);
  suggestionCount = signal(0);

  selected = signal<Set<number>>(new Set());
  keepId = signal<number | null>(null);
  mergeName = signal('');

  /** Which row's name is being edited inline, if any. */
  editingId = signal<number | null>(null);
  editingName = signal('');

  readonly fmt = formatMoney;

  chosen = computed(() => this.rows().filter((r) => this.selected().has(r.id)));
  keepRow = computed(
    () => this.chosen().find((r) => r.id === this.keepId()) ?? null,
  );
  canMerge = computed(
    () => this.chosen().length >= 2 && this.keepId() !== null,
  );

  /** Combined spend of the selection — the reason to bother merging. */
  selectedTotal = computed(() =>
    this.chosen()
      .reduce((sum, r) => sum + Number(r.total_spent), 0)
      .toFixed(2),
  );

  constructor() {
    this.load();
    this.api.suggestions().subscribe({
      next: (s) => this.suggestionCount.set(s.length),
      error: () => {},
    });
  }

  load(): void {
    this.loading.set(true);
    this.api.merchants(this.search(), this.sort(), 150).subscribe({
      next: (page) => {
        this.rows.set(page.rows);
        this.total.set(page.total);
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
    this.clearSelection();
    this.load();
  }

  onSort(value: string): void {
    this.sort.set(value as MerchantSort);
    this.load();
  }

  toggle(row: MerchantRow): void {
    this.selected.update((set) => {
      const next = new Set(set);
      if (next.has(row.id)) next.delete(row.id);
      else next.add(row.id);
      return next;
    });

    const chosen = this.chosen();
    if (!chosen.length) {
      this.keepId.set(null);
      this.mergeName.set('');
      return;
    }
    if (!this.selected().has(this.keepId() ?? -1)) {
      // Default to the biggest by spend — usually the one worth keeping.
      this.keep(chosen[0]);
    }
  }

  keep(row: MerchantRow): void {
    this.keepId.set(row.id);
    this.mergeName.set(row.canonical_name);
  }

  clearSelection(): void {
    this.selected.set(new Set());
    this.keepId.set(null);
    this.mergeName.set('');
  }

  merge(): void {
    const keepId = this.keepId();
    if (!keepId || !this.canMerge()) return;
    const sources = this.chosen().filter((r) => r.id !== keepId);
    const name = this.mergeName().trim();

    if (
      !confirm(
        `Merge ${sources.length} merchant(s) into “${name}”?\n\n` +
          sources.map((s) => `  • ${s.canonical_name}`).join('\n') +
          '\n\nTransactions move across. This cannot be undone automatically.',
      )
    ) {
      return;
    }

    this.busy.set(true);
    this.error.set(null);
    this.api
      .mergeMany(
        sources.map((s) => s.id),
        keepId,
        name || undefined,
      )
      .subscribe({
        next: (m) => {
          this.notice.set(
            `Merged ${sources.length} into “${m.canonical_name}” — ${m.transaction_count} transactions.`,
          );
          this.busy.set(false);
          this.clearSelection();
          this.load();
          setTimeout(() => this.notice.set(null), 5000);
        },
        error: (e) => {
          this.error.set(this.describe(e));
          this.busy.set(false);
        },
      });
  }

  startEdit(row: MerchantRow): void {
    this.editingId.set(row.id);
    this.editingName.set(row.canonical_name);
  }

  cancelEdit(): void {
    this.editingId.set(null);
    this.editingName.set('');
  }

  saveEdit(row: MerchantRow): void {
    const name = this.editingName().trim();
    if (!name || name === row.canonical_name) {
      this.cancelEdit();
      return;
    }
    this.busy.set(true);
    this.api.renameMerchant(row.id, name).subscribe({
      next: (m) => {
        this.rows.update((rows) =>
          rows.map((r) =>
            r.id === m.id ? { ...r, canonical_name: m.canonical_name } : r,
          ),
        );
        this.busy.set(false);
        this.cancelEdit();
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
