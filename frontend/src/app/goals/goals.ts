import { Component, effect, inject, signal, untracked } from '@angular/core';

import { Api } from '../api';
import { GoalInput, GoalPlan, GoalRow } from '../models';
import { fromCents, toCents } from '../money';
import { Sync } from '../sync';
import { UsdPipe } from '../usd';

/**
 * What you are saving for, in order, and whether you are on track.
 *
 * Goals share one pot — cash after the cards, and investments that are not
 * retirement money — split in list order, cash first. What is still needed
 * comes from what a typical month adds to that pot. Order is priority: move
 * a goal up and it is funded before the ones below it.
 *
 * Every figure is worked out by the server. Editing a goal saves it as soon
 * as the box is left, and the whole plan comes back.
 */
@Component({
  selector: 'app-goals',
  imports: [UsdPipe],
  templateUrl: './goals.html',
  styleUrl: './goals.scss',
})
export class Goals {
  private api = inject(Api);
  private sync = inject(Sync);

  plan = signal<GoalPlan | null>(null);
  error = signal<string | null>(null);

  // The add row.
  newName = signal('');
  newSize = signal('');
  newInMonths = signal(false);
  newDate = signal('');

  constructor() {
    this.load();
    effect(() => {
      if (this.sync.version()) untracked(() => this.load());
    });
  }

  // --- reading -----------------------------------------------------------------

  /** "June 2029" from "2029-06-01". Parsed as local, never `new Date(iso)`. */
  month(iso: string): string {
    const [y, m] = iso.split('-').map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
  }

  /** How far past its date a goal lands, in months. */
  late(g: GoalRow): number {
    if (!g.target_date || !g.reached_on) return 0;
    const [ty, tm] = g.target_date.split('-').map(Number);
    const [ry, rm] = g.reached_on.split('-').map(Number);
    return (ry - ty) * 12 + (rm - tm);
  }

  covered(g: GoalRow): boolean {
    return g.remaining === '0.00';
  }

  isZero(value: string): boolean {
    return /^-?[0.]*$/.test(value);
  }

  /** Share of the goal already covered, for the bar. Display only. */
  pct(part: string, g: GoalRow): number {
    const target = Number(g.target);
    return target > 0 ? Math.min(100, (Number(part) / target) * 100) : 0;
  }

  // --- editing -----------------------------------------------------------------

  rename(g: GoalRow, name: string): void {
    if (!name.trim() || name.trim() === g.name) return;
    this.save(g, { name: name.trim() });
  }

  resize(g: GoalRow, value: string): void {
    const cents = toCents(value);
    if (cents === null || cents < 0n) return this.load();
    const amount = fromCents(cents);
    if (g.months_of_spending !== null) {
      this.save(g, { months_of_spending: amount });
    } else {
      this.save(g, { amount });
    }
  }

  redate(g: GoalRow, value: string): void {
    this.save(g, { target_date: value ? `${value}-01` : null });
  }

  move(g: GoalRow, by: -1 | 1): void {
    const ids = this.plan()!.goals.map((x) => x.id);
    const i = ids.indexOf(g.id);
    const j = i + by;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    this.api.reorderGoals(ids).subscribe(this.handle());
  }

  remove(g: GoalRow): void {
    this.api.deleteGoal(g.id).subscribe(this.handle());
  }

  add(): void {
    const cents = toCents(this.newSize());
    if (!this.newName().trim() || cents === null || cents < 0n) return;
    const size = fromCents(cents);
    this.api
      .addGoal({
        name: this.newName().trim(),
        amount: this.newInMonths() ? null : size,
        months_of_spending: this.newInMonths() ? size : null,
        target_date: this.newDate() ? `${this.newDate()}-01` : null,
      })
      .subscribe({
        next: (plan) => {
          this.plan.set(plan);
          this.newName.set('');
          this.newSize.set('');
          this.newDate.set('');
          this.newInMonths.set(false);
        },
        error: (e) => this.error.set(e?.error?.detail ?? 'That did not save.'),
      });
  }

  private save(g: GoalRow, patch: Partial<GoalInput>): void {
    const body: GoalInput = {
      name: g.name,
      amount: g.amount,
      months_of_spending: g.months_of_spending,
      target_date: g.target_date,
      ...patch,
    };
    this.api.updateGoal(g.id, body).subscribe(this.handle());
  }

  private handle() {
    return {
      next: (plan: GoalPlan) => {
        this.error.set(null);
        this.plan.set(plan);
      },
      error: (e: { error?: { detail?: string } }) =>
        this.error.set(e?.error?.detail ?? 'That did not save.'),
    };
  }

  private load(): void {
    this.api.goals().subscribe(this.handle());
  }
}
