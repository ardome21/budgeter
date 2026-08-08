import { Component, inject, input, model, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import { MerchantKey } from '../models';

/**
 * Pick who was paid, from the names already in use.
 *
 * This is the whole replacement for the merge queue. The queue existed because
 * merchants were resolved from the description on write, so a second spelling
 * could only be discovered — and fixed — after it was already in the data.
 * Offering what exists at the moment of entry means the second spelling never
 * arrives, and there is nothing to reconcile later.
 *
 * A free-typed name is still allowed: a shop the database has never seen has
 * to be nameable. The backend snaps anything that differs only by case onto
 * the existing spelling, so 'harris teeter' joins Harris Teeter rather than
 * starting a rival.
 *
 * Backed by a native `<datalist>` on purpose — it gives keyboard handling,
 * filtering and dismissal that a hand-rolled dropdown would have to reimplement
 * and get wrong.
 */
@Component({
  selector: 'app-merchant-field',
  imports: [FormsModule],
  template: `
    <input
      type="text"
      [attr.list]="listId"
      [attr.id]="inputId() || null"
      [placeholder]="placeholder()"
      [ngModel]="value()"
      (ngModelChange)="onType($event)"
      (focus)="load(value() ?? '')"
      [attr.aria-label]="label()"
    />
    <datalist [id]="listId">
      @for (k of options(); track k.key) {
        <option [value]="k.key">{{ k.count }} so far</option>
      }
    </datalist>
  `,
  styles: `
    :host {
      display: contents;
    }
  `,
})
export class MerchantField {
  private api = inject(Api);

  value = model<string | null>(null);
  label = input('Merchant');
  placeholder = input('Merchant');
  inputId = input<string>('');

  options = signal<MerchantKey[]>([]);

  // Datalists are matched to inputs by id, so two on one page must not share
  // one — the import preview renders a field per row.
  readonly listId = `merchants-${Math.random().toString(36).slice(2, 9)}`;

  private lastQuery: string | null = null;

  onType(next: string): void {
    this.value.set(next);
    this.load(next);
  }

  /** Refill the options. Skipped when the query has not actually changed. */
  load(q: string): void {
    const query = q.trim();
    if (query === this.lastQuery) return;
    this.lastQuery = query;
    this.api.merchantKeys(query).subscribe({
      next: (keys) => this.options.set(keys),
      // A picker that cannot reach the API still has to accept typing.
      error: () => this.options.set([]),
    });
  }
}
