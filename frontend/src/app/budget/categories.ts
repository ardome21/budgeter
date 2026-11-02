import { Component, inject, output, signal } from '@angular/core';

import { Api } from '../api';
import { Category, CategoryKind } from '../models';

/**
 * The categories themselves: rename, re-kind, add, and delete the empty ones.
 *
 * It lives at the foot of the Budget page because that is the only place a
 * category is a decision. It used to be a Settings screen of its own, with
 * one table on it, which nobody had a reason to open.
 */
@Component({
  selector: 'app-categories',
  template: `
    <h2>Categories</h2>
    <p class="dim small">
      <strong>Spending</strong> is money that left for good.
      <strong>Transfer</strong> is money moved between your own accounts — a
      card payment, cash into savings — and is in no spending total.
      <strong>Savings</strong> is a transfer worth seeing on its own, because
      you still have it.
    </p>

    @if (error()) {
      <p class="banner error">{{ error() }}</p>
    }

    <div class="card scroll-x">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Kind</th>
            <th class="num">Transactions</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          @for (c of categories(); track c.id) {
            <tr>
              <td>
                <!-- Saved on leaving the box, not on every key. -->
                <input type="text" class="name" [value]="c.name"
                       (change)="rename(c, $any($event.target))"
                       (keydown.enter)="$any($event.target).blur()"
                       [attr.aria-label]="'Name of ' + c.name" />
              </td>
              <td>
                <select [value]="c.kind" (change)="rekind(c, $any($event.target).value)"
                        [attr.aria-label]="'Kind of ' + c.name">
                  @for (k of kinds; track k) {
                    <option [value]="k" [selected]="k === c.kind">{{ k.toLowerCase() }}</option>
                  }
                </select>
              </td>
              <td class="num dim">{{ c.transaction_count || '—' }}</td>
              <td class="actions">
                <!-- Offered only where it would work: the server refuses to
                     delete a category with rows filed under it. -->
                @if (!c.transaction_count) {
                  <button type="button" class="link-button" (click)="remove(c)">Delete</button>
                }
              </td>
            </tr>
          }
        </tbody>
        <tfoot>
          <tr>
            <td>
              <input type="text" class="name" placeholder="New category"
                     [value]="newName()" (input)="newName.set($any($event.target).value)"
                     (keydown.enter)="add()" aria-label="New category name" />
            </td>
            <td>
              <select [value]="newKind()" (change)="newKind.set($any($event.target).value)"
                      aria-label="New category kind">
                @for (k of kinds; track k) {
                  <option [value]="k" [selected]="k === newKind()">{{ k.toLowerCase() }}</option>
                }
              </select>
            </td>
            <td></td>
            <td class="actions">
              <button type="button" (click)="add()" [disabled]="!newName().trim()">Add</button>
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  `,
  styles: `
    h2 { font-size: 15px; font-weight: 600; margin: 2rem 0 0.2rem; }
    .small { font-size: 13px; max-width: 44rem; }
    .name { width: 100%; max-width: 16rem; }
    .actions { width: 1%; text-align: right; white-space: nowrap; }
    tfoot td { border-top: 1px solid var(--border); }
    .link-button {
      all: unset; cursor: pointer; font-size: 13px; color: var(--text-dim);
      border-bottom: 1px solid var(--border);
      &:hover { color: var(--over); border-bottom-color: var(--over); }
      &:focus-visible { outline: 2px solid var(--accent-dim); }
    }
  `,
})
export class Categories {
  private api = inject(Api);

  /** Something about the categories moved, so the plan above is stale. */
  changed = output<void>();

  categories = signal<Category[]>([]);
  error = signal<string | null>(null);
  newName = signal('');
  newKind = signal<CategoryKind>('SPENDING');

  readonly kinds: CategoryKind[] = ['SPENDING', 'INCOME', 'TRANSFER', 'SAVINGS'];

  constructor() {
    this.load();
  }

  rename(category: Category, box: HTMLInputElement): void {
    const name = box.value.trim();
    if (!name || name === category.name) {
      box.value = category.name;
      return;
    }
    this.save(category, { name }, () => (box.value = category.name));
  }

  rekind(category: Category, kind: CategoryKind): void {
    if (kind !== category.kind) this.save(category, { kind });
  }

  add(): void {
    const name = this.newName().trim();
    if (!name) return;
    this.error.set(null);
    this.api.createCategory({ name, kind: this.newKind(), sort_order: 100 }).subscribe({
      next: () => {
        this.newName.set('');
        this.reload();
      },
      error: (e) => this.error.set(describe(e)),
    });
  }

  remove(category: Category): void {
    if (!confirm(`Delete ${category.name}? Its budget goes with it.`)) return;
    this.error.set(null);
    this.api.deleteCategory(category.id).subscribe({
      next: () => this.reload(),
      error: (e) => this.error.set(describe(e)),
    });
  }

  /** PATCH takes the whole category, so the unchanged fields ride along. */
  private save(category: Category, change: Partial<Category>, undo?: () => void): void {
    this.error.set(null);
    const { name, kind, sort_order } = { ...category, ...change };
    this.api.updateCategory(category.id, { name, kind, sort_order }).subscribe({
      next: () => this.reload(),
      error: (e) => {
        undo?.();
        this.error.set(describe(e));
      },
    });
  }

  private reload(): void {
    this.load();
    this.changed.emit();
  }

  private load(): void {
    this.api.categories().subscribe((c) => this.categories.set(c));
  }
}

function describe(err: unknown): string {
  return (err as { error?: { detail?: string } })?.error?.detail ?? 'That did not save.';
}
