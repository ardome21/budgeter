import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { Api } from '../api';
import { Category, CategoryKind } from '../models';

/** Categories: adding and deleting them. Budgets live on the Budget page. */
@Component({
  selector: 'app-settings',
  imports: [FormsModule, RouterLink],
  templateUrl: './settings.html',
  styleUrl: './settings.scss',
})
export class Settings {
  private api = inject(Api);

  categories = signal<Category[]>([]);
  error = signal<string | null>(null);

  newName = signal('');
  newKind = signal<CategoryKind>('SPENDING');

  readonly kinds: CategoryKind[] = ['SPENDING', 'INCOME', 'TRANSFER', 'SAVINGS'];

  constructor() {
    this.load();
  }

  load(): void {
    this.api.categories().subscribe((c) => this.categories.set(c));
  }

  addCategory(): void {
    const name = this.newName().trim();
    if (!name) return;
    this.api
      .createCategory({ name, kind: this.newKind(), sort_order: 100 })
      .subscribe({
        next: () => {
          this.newName.set('');
          this.load();
        },
        error: (e) => this.error.set(this.describe(e)),
      });
  }

  removeCategory(category: Category): void {
    if (!confirm(`Delete ${category.name}?`)) return;
    this.api.deleteCategory(category.id).subscribe({
      next: () => this.load(),
      // Refused while transactions are filed here, and the message says how
      // many — which is the useful half of the answer.
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  private describe(err: unknown): string {
    return (
      (err as { error?: { detail?: string } })?.error?.detail ??
      'That did not save.'
    );
  }
}
