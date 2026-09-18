import { Component, inject, input, model, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';

/**
 * Pick who was paid, from the names already in use.
 *
 * This is the whole replacement for a merge queue. A queue was needed when
 * merchants were resolved from the bank's descriptor on write, because a second
 * spelling could only be discovered after it was already in the data. Offering
 * what exists at the moment of entry means the second spelling never arrives,
 * and there is nothing to reconcile afterwards.
 *
 * A free-typed name is still allowed — a shop the database has never seen has
 * to be nameable. The server snaps anything differing only by case onto the
 * existing spelling, so 'harris teeter' joins Harris Teeter rather than
 * starting a rival to it.
 *
 * Backed by a native `<datalist>` on purpose: it brings keyboard handling,
 * filtering and dismissal that a hand-rolled dropdown would have to
 * reimplement and would get wrong.
 */
@Component({
  selector: 'app-merchant-field',
  imports: [FormsModule],
  template: `
    <input
      type="text"
      [attr.list]="listId"
      [placeholder]="placeholder()"
      [ngModel]="value()"
      (ngModelChange)="value.set($event)"
      (blur)="committed.emit(value() ?? '')"
      [attr.aria-label]="label()"
    />
    <datalist [id]="listId">
      @for (name of names(); track name) {
        <option [value]="name"></option>
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

  /**
   * Fired on blur rather than on every keystroke. A save per character would
   * write a row per letter typed, and the name is only meaningful once it is
   * finished.
   */
  committed = output<string>();

  /**
   * Loaded once and shared by every instance on the page. The transactions
   * screen renders one of these per row; fetching the list two hundred times
   * to show the same two hundred names is the kind of thing that makes a
   * screen feel slow for no reason.
   */
  private static cache = signal<string[]>([]);
  private static loaded = false;
  names = MerchantField.cache;

  readonly listId = `merchants-${Math.random().toString(36).slice(2, 9)}`;

  constructor() {
    if (!MerchantField.loaded) {
      MerchantField.loaded = true;
      this.api.merchants().subscribe({
        next: (names) => MerchantField.cache.set(names),
        // A picker that cannot reach the API must still accept typing.
        error: () => MerchantField.cache.set([]),
      });
    }
  }
}
