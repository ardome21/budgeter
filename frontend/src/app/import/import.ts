import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import {
  AccountRow,
  Category,
  CommitResult,
  Preview,
  PreviewRow,
  formatMoney,
} from '../models';

/** A preview row plus the choices made about it on screen. */
interface ReviewRow extends PreviewRow {
  categoryId: number | null;
  include: boolean;
}

@Component({
  selector: 'app-import',
  imports: [FormsModule],
  templateUrl: './import.html',
  styleUrl: './import.scss',
})
export class Import {
  private api = inject(Api);

  categories = signal<Category[]>([]);
  /** Only open accounts — a settled one cannot receive new charges. */
  accounts = signal<AccountRow[]>([]);
  accountId = signal<number | null>(null);
  pasted = signal('');
  flipSign = signal(false);
  preview = signal<Preview | null>(null);
  review = signal<ReviewRow[]>([]);
  result = signal<CommitResult | null>(null);
  busy = signal(false);
  error = signal<string | null>(null);
  fileName = signal<string | null>(null);

  readonly fmt = formatMoney;

  /** Flattened for the template — avoids pulling in KeyValuePipe for one line. */
  detectedColumns = computed(() =>
    Object.entries(this.preview()?.detected_columns ?? {}).map(
      ([key, value]) => ({
        key,
        value,
      }),
    ),
  );

  includedCount = computed(() => this.review().filter((r) => r.include).length);
  needsCategory = computed(
    () => this.review().filter((r) => r.include && !r.categoryId).length,
  );
  /** Ticked rows that match something already on file closely enough to check. */
  flaggedCount = computed(
    () => this.review().filter((r) => r.include && r.near_duplicates.length).length,
  );
  canCommit = computed(
    () => this.includedCount() > 0 && this.needsCategory() === 0,
  );

  constructor() {
    this.api.categories().subscribe((c) => this.categories.set(c));
    this.api
      .accounts()
      .subscribe((a) => this.accounts.set(a.filter((x) => !x.closed_on)));
  }

  setAccount(value: string): void {
    this.accountId.set(value ? Number(value) : null);
  }

  onFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    this.fileName.set(file.name);
    this.run(this.api.previewCsvFile(file, this.flipSign(), this.accountId()));
  }

  previewPaste(): void {
    if (!this.pasted().trim()) {
      this.error.set('Paste some CSV first.');
      return;
    }
    this.fileName.set(null);
    this.run(this.api.previewCsv(this.pasted(), this.flipSign(), this.accountId()));
  }

  private run(request: ReturnType<Api['previewCsv']>): void {
    this.busy.set(true);
    this.error.set(null);
    this.result.set(null);
    request.subscribe({
      next: (p) => {
        this.preview.set(p);
        this.review.set(
          p.rows.map((r) => ({
            ...r,
            categoryId: r.suggested_category_id,
            // Duplicates start unticked — the whole point of detecting them.
            include: r.duplicate_of === null,
          })),
        );
        this.busy.set(false);
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.busy.set(false);
      },
    });
  }

  setCategory(row: ReviewRow, value: string | number): void {
    this.review.update((rows) =>
      rows.map((r) =>
        r.row_number === row.row_number
          ? { ...r, categoryId: Number(value) }
          : r,
      ),
    );
  }

  toggle(row: ReviewRow): void {
    this.review.update((rows) =>
      rows.map((r) =>
        r.row_number === row.row_number ? { ...r, include: !r.include } : r,
      ),
    );
  }

  /** Apply one category to every row still missing one. */
  fillBlanks(value: string): void {
    if (!value) return;
    this.review.update((rows) =>
      rows.map((r) => (r.categoryId ? r : { ...r, categoryId: Number(value) })),
    );
  }

  /**
   * Drop every flagged row in one go — the answer when an export overlaps a
   * period already on file. Flagged rows stay ticked by default because a
   * near match is genuinely ambiguous, and quietly discarding real spending is
   * a worse failure than a duplicate you can see and delete.
   */
  untickFlagged(): void {
    this.review.update((rows) =>
      rows.map((r) => (r.near_duplicates.length ? { ...r, include: false } : r)),
    );
  }

  commit(): void {
    const rows = this.review()
      .filter((r) => r.include && r.categoryId)
      .map((r) => ({
        occurred_on: r.occurred_on,
        raw_description: r.raw_description,
        amount: r.amount,
        category_id: r.categoryId,
        import_hash: r.import_hash,
      }));
    if (!rows.length) return;

    this.busy.set(true);
    this.error.set(null);
    this.api.commitImport(rows, this.accountId()).subscribe({
      next: (res) => {
        this.result.set(res);
        this.preview.set(null);
        this.review.set([]);
        this.pasted.set('');
        this.busy.set(false);
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.busy.set(false);
      },
    });
  }

  reset(): void {
    this.preview.set(null);
    this.review.set([]);
    this.result.set(null);
    this.error.set(null);
    this.fileName.set(null);
  }

  private describe(e: unknown): string {
    const err = e as { status?: number; error?: { detail?: unknown } };
    if (err?.status === 0)
      return 'Cannot reach the API. Is the backend running?';
    const detail = err?.error?.detail;
    return typeof detail === 'string' ? detail : 'Import failed.';
  }
}
