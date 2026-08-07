import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Observable, forkJoin, of } from 'rxjs';
import { map } from 'rxjs/operators';

import { Api } from '../api';
import { Suggestion } from '../models';

/**
 * The merchant review queue.
 *
 * A proposal answers "these share a brand — are they one place?", which is a
 * question, not a claim. So every group is decided by a person, and a "no" is
 * recorded so the same pair is never proposed again. A queue you cannot empty
 * is one nobody works through.
 *
 * Members are ticked individually because a shared brand is not always one
 * place: Uber Eats is Food and Drinks, Uber Trip is Transportation.
 */
@Component({
  selector: 'app-merchants',
  imports: [FormsModule, RouterLink],
  templateUrl: './merchants.html',
  styleUrl: './merchants.scss',
})
export class Merchants {
  private api = inject(Api);

  queue = signal<Suggestion[]>([]);
  index = signal(0);
  ticked = signal<Set<number>>(new Set());
  keepId = signal<number | null>(null);
  loading = signal(true);
  busy = signal(false);
  error = signal<string | null>(null);
  done = signal(0);

  skipped = signal(0);

  /** What the surviving merchant will be called. Free text, not a picker. */
  name = signal('');

  current = computed(() => this.queue()[this.index()] ?? null);

  /** Groups still awaiting a decision, including any skipped this session. */
  remaining = computed(() => Math.max(this.queue().length - this.index(), 0));

  keepName = computed(
    () =>
      this.current()?.members.find((m) => m.id === this.keepId())
        ?.canonical_name ?? '',
  );

  /** Ticked members other than the survivor — the ones that actually move. */
  toMerge = computed(() =>
    (this.current()?.members ?? []).filter(
      (m) => this.ticked().has(m.id) && m.id !== this.keepId(),
    ),
  );

  leftOut = computed(() =>
    (this.current()?.members ?? []).filter((m) => !this.ticked().has(m.id)),
  );

  constructor() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.api.suggestions().subscribe({
      next: (rows) => {
        this.queue.set(rows);
        this.index.set(0);
        this.skipped.set(0);
        this.reset();
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.loading.set(false);
      },
    });
  }

  /** Every member ticked, biggest kept — the answer for a clean group. */
  private reset(): void {
    const group = this.current();
    if (!group) {
      this.ticked.set(new Set());
      this.keepId.set(null);
      return;
    }
    this.ticked.set(new Set(group.members.map((m) => m.id)));
    this.keepId.set(group.members[0]?.id ?? null);
    this.name.set(group.members[0]?.canonical_name ?? '');
  }

  toggle(id: number): void {
    this.ticked.update((set) => {
      const next = new Set(set);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    // The survivor has to be one of the ticked members.
    if (!this.ticked().has(this.keepId() ?? -1)) {
      const first = this.current()?.members.find((m) =>
        this.ticked().has(m.id),
      );
      this.keepId.set(first?.id ?? null);
    }
  }

  keep(id: number): void {
    this.keepId.set(id);
    this.ticked.update((set) => new Set(set).add(id));
    const member = this.current()?.members.find((m) => m.id === id);
    if (member) this.name.set(member.canonical_name);
  }

  /** Merge the ticked members, then record that the untouched ones differ. */
  accept(): void {
    const keepId = this.keepId();
    const group = this.current();
    if (!group || keepId === null || !this.toMerge().length) return;

    this.busy.set(true);
    this.error.set(null);
    const merges = this.toMerge().map((m) =>
      this.api.mergeMerchant(m.id, keepId),
    );

    const typed = this.name().trim();
    const others = this.leftOut().map((m) => m.canonical_name);

    forkJoin(merges).subscribe({
      next: () => {
        // Rename before recording rejections: splits are keyed by name, and
        // they must be written against the name that survives.
        const renamed: Observable<string> =
          typed && typed !== this.keepName()
            ? this.api
                .renameMerchant(keepId, typed)
                .pipe(map((m) => m.canonical_name))
            : of(this.keepName());

        renamed.subscribe({
          next: (survivor) => {
            const after: Observable<void> = others.length
              ? this.api.rejectSuggestion(others, survivor)
              : of(void 0);
            after.subscribe({
              next: () => {
                this.done.update((n) => n + 1);
                this.busy.set(false);
                this.next();
              },
              error: (e) => {
                this.error.set(this.describe(e));
                this.busy.set(false);
                this.next();
              },
            });
          },
          error: (e) => {
            // The merge already happened; only the rename failed, so say so
            // and stay put rather than pretending the group is undecided.
            this.error.set(this.describe(e));
            this.busy.set(false);
            this.load();
          },
        });
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.busy.set(false);
      },
    });
  }

  /** None of these are the same place. Never propose this group again. */
  rejectAll(): void {
    const group = this.current();
    if (!group) return;
    this.busy.set(true);
    this.api
      .rejectSuggestion(group.members.map((m) => m.canonical_name))
      .subscribe({
        next: () => {
          this.done.update((n) => n + 1);
          this.busy.set(false);
          this.next();
        },
        error: (e) => {
          this.error.set(this.describe(e));
          this.busy.set(false);
        },
      });
  }

  /** Decide later. Nothing recorded, so it comes back next time. */
  skip(): void {
    this.skipped.update((n) => n + 1);
    this.next();
  }

  private next(): void {
    this.index.update((i) => i + 1);
    this.reset();
  }

  private describe(e: unknown): string {
    const err = e as { status?: number; error?: { detail?: string } };
    if (err?.status === 0)
      return 'Cannot reach the API. Is the backend running?';
    return err?.error?.detail ?? 'Something went wrong.';
  }
}
