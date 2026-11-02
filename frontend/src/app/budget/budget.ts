import { Component, computed, inject, signal } from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { Api } from '../api';
import { Budget, BudgetPlan, BudgetPlanRow } from '../models';
import { fromCents, toCents } from '../money';
import { UsdPipe } from '../usd';
import { Categories } from './categories';
import { Regular } from './regular';

/** Where a column of proposals comes from. `current` is the budget in force. */
type Source = 'last' | 'typical' | 'current';

/**
 * Set a month's budget in one sitting, from what you actually spent.
 *
 * Every spending category is listed with what it cost last month and in a
 * typical month (the median of three), and the boxes start filled from one of
 * them, rounded up to the next $10 by the server. Clicking either figure takes
 * it for that row. Nothing is written until Save.
 *
 * Money in is planned the same way, and the strip at the top sets one against
 * the other: what comes in, what the plan spends, and what that leaves.
 *
 * A saved budget holds for every later month until one is given its own, so
 * this is a page you visit when something changes, not every month. Budgets
 * went unmaintained for a year in the version this replaced because each
 * month started empty.
 */
@Component({
  selector: 'app-budget',
  imports: [NgTemplateOutlet, RouterLink, UsdPipe, Categories, Regular],
  templateUrl: './budget.html',
  styleUrl: './budget.scss',
})
export class BudgetPage {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  /** YYYY-MM, as the month input speaks it. */
  month = signal(this.route.snapshot.queryParamMap.get('month') ?? thisMonth());
  plan = signal<BudgetPlan | null>(null);
  amounts = signal<Record<number, string>>({});
  source = signal<Source>('last');
  saving = signal(false);
  saved = signal(false);
  error = signal<string | null>(null);
  /** Bumped when a category is added, renamed or removed. */
  categoryVersion = signal(0);

  /** The budget in force was set for this very month, not carried into it. */
  ownBudget = computed(() => {
    const p = this.plan();
    return !!p?.budget_from && p.budget_from === p.month;
  });

  /** Rows whose box holds something that is not an amount. Save waits on them. */
  invalid = computed(() => {
    const bad = new Set<number>();
    for (const [id, value] of Object.entries(this.amounts())) {
      if (value.trim() && toCents(value) === null) bad.add(Number(id));
    }
    return bad;
  });

  private incomeIds = computed(() => new Set(this.plan()?.income.map((r) => r.category_id)));

  /** The spending plan's total, summed in whole cents as it is typed. */
  total = computed(() => fromCents(this.sum((id) => !this.incomeIds().has(id)) ?? 0n));

  /** Expected money in, as typed. Null when every income box is blank. */
  incomeTotal = computed(() => {
    const cents = this.sum((id) => this.incomeIds().has(id));
    return cents === null ? null : fromCents(cents);
  });

  /** Income is blank, so the strip is reckoned against last month's instead — and says so. */
  incomeAssumed = computed(() => this.incomeTotal() === null);

  /** Money in less the spending plan. Negative means it spends more than comes in. */
  left = computed(() => {
    const p = this.plan();
    if (!p) return null;
    const income = toCents(this.incomeTotal() ?? p.last_month_income) ?? 0n;
    return fromCents(income - (toCents(this.total()) ?? 0n));
  });

  /** `left` is negative. Read off the sign of the exact string, never compared as a number. */
  short = computed(() => this.left()?.startsWith('-') ?? false);

  constructor() {
    this.load();
  }

  changeMonth(value: string): void {
    if (!value) return;
    this.month.set(value);
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { month: value },
      replaceUrl: true,
    });
    this.load();
  }

  /** Refill every box from one column. Typed changes are replaced — that is the point of the switch. */
  fillFrom(source: Source): void {
    this.source.set(source);
    const next: Record<number, string> = {};
    const p = this.plan();
    for (const row of [...(p?.income ?? []), ...(p?.rows ?? [])]) {
      const value = this.proposal(row, source);
      if (value !== null) next[row.category_id] = value;
    }
    this.amounts.set(next);
  }

  /** Take one figure for one row. */
  use(row: BudgetPlanRow, source: Source): void {
    const value = this.proposal(row, source);
    this.amounts.update((a) => ({ ...a, [row.category_id]: value ?? '' }));
  }

  setAmount(id: number, value: string): void {
    this.amounts.update((a) => ({ ...a, [id]: value }));
  }

  amountFor(id: number): string {
    return this.amounts()[id] ?? '';
  }

  save(): void {
    if (this.invalid().size) return;
    const [year, month] = this.month().split('-').map(Number);
    const rows: Budget[] = Object.entries(this.amounts())
      // A blank is "no target", which is a different statement from zero and
      // is expressed by leaving the category out entirely.
      .filter(([, value]) => value.trim() !== '')
      .map(([id, value]) => ({ category_id: Number(id), amount: fromCents(toCents(value)!) }));
    this.saving.set(true);
    this.api.setBudgets(year, month, rows).subscribe({
      next: () => {
        this.saving.set(false);
        this.saved.set(true);
        setTimeout(() => this.saved.set(false), 2500);
        this.load(false);
      },
      error: (e) => {
        this.saving.set(false);
        this.error.set(e?.error?.detail ?? 'That did not save.');
      },
    });
  }

  /** "October 2026" from "2026-10-01" or "2026-10". Parsed as local, never `new Date(iso)`. */
  label(iso: string, withYear = true): string {
    const [y, m] = iso.split('-').map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString(undefined, {
      month: 'long',
      ...(withYear ? { year: 'numeric' } : {}),
    });
  }

  /** Whole cents across the boxes `include` picks; null when all of them are blank. */
  private sum(include: (id: number) => boolean): bigint | null {
    let cents: bigint | null = null;
    for (const [id, value] of Object.entries(this.amounts())) {
      const amount = toCents(value);
      if (amount === null || !include(Number(id))) continue;
      cents = (cents ?? 0n) + amount;
    }
    return cents;
  }

  private proposal(row: BudgetPlanRow, source: Source): string | null {
    if (source === 'current') return row.budget;
    return source === 'last' ? row.from_last_month : row.from_typical;
  }

  /** A category was renamed, added or re-kinded: fetch the rows again, and
   *  keep whatever is typed in the boxes. */
  reload(): void {
    this.load(false);
  }

  /** `refill` false keeps what is in the boxes — after a save they are the budget. */
  private load(refill = true): void {
    const [year, month] = this.month().split('-').map(Number);
    this.error.set(null);
    this.api.budgetPlan(year, month).subscribe({
      next: (plan) => {
        this.plan.set(plan);
        // With a budget in force, start from it: you came to adjust, not to
        // start over. Without one, start from last month, as asked.
        if (refill) this.fillFrom(plan.budget_from ? 'current' : 'last');
      },
      error: () => this.error.set('Could not load the budget.'),
    });
  }
}

function thisMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}
