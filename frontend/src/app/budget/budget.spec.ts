import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { Api } from '../api';
import { Budget, BudgetPlan } from '../models';
import { BudgetPage } from './budget';

function plan(budget_from: string | null = null): BudgetPlan {
  return {
    month: '2026-11-01',
    budget_from,
    last_month: '2026-10-01',
    last_month_spent: '1448.24',
    last_month_income: '2000.00',
    typical_spent: '1206.91',
    income: [
      {
        category_id: 9, category_name: 'Income', budget: budget_from ? '4900.00' : null,
        last_month: '2000.00', typical: '1995.00', months_seen: 3,
        from_last_month: '2000.00', from_typical: '1990.00',
      },
    ],
    rows: [
      {
        category_id: 1, category_name: 'Rent', budget: budget_from ? '1500.00' : null,
        last_month: '1606.07', typical: '1597.12', months_seen: 3,
        from_last_month: '1610.00', from_typical: '1600.00',
      },
      {
        category_id: 2, category_name: 'Shopping', budget: null,
        last_month: '-157.83', typical: '-390.21', months_seen: 3,
        from_last_month: null, from_typical: null,
      },
      {
        category_id: 3, category_name: 'Travel', budget: null,
        last_month: '0.00', typical: '0.00', months_seen: 1,
        from_last_month: null, from_typical: null,
      },
    ],
  };
}

describe('BudgetPage', () => {
  let saved: Budget[] | null;

  function render(p: BudgetPlan) {
    saved = null;
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        {
          provide: Api,
          useValue: {
            budgetPlan: () => of(p),
            categories: () => of([]),
            setBudgets: (_y: number, _m: number, rows: Budget[]) => {
              saved = rows;
              return of(rows);
            },
          },
        },
      ],
    });
    const fixture = TestBed.createComponent(BudgetPage);
    fixture.detectChanges();
    return fixture;
  }

  it('starts from last month when no budget has been set, leaving unspent rows blank', () => {
    const page = render(plan()).componentInstance;
    expect(page.source()).toBe('last');
    expect(page.amounts()).toEqual({ 1: '1610.00', 9: '2000.00' });
    expect(page.total()).toBe('1610.00');
    expect(page.incomeTotal()).toBe('2000.00');
    expect(page.left()).toBe('390.00');
    expect(page.short()).toBeFalse();
  });

  it('starts from the budget in force when there is one', () => {
    const page = render(plan('2026-09-01')).componentInstance;
    expect(page.source()).toBe('current');
    expect(page.amounts()).toEqual({ 1: '1500.00', 9: '4900.00' });
  });

  it('totals what is typed exactly, and holds Save while a box is not an amount', () => {
    const page = render(plan()).componentInstance;
    page.setAmount(3, '0.10');
    page.setAmount(2, '0.20');
    expect(page.total()).toBe('1610.30');
    page.setAmount(2, 'lots');
    expect(page.invalid().has(2)).toBeTrue();
    page.save();
    expect(saved).toBeNull();
  });

  it('saves typed amounts as plain decimals and leaves blanks out', () => {
    const page = render(plan()).componentInstance;
    page.setAmount(1, '$1,650');
    page.setAmount(3, '');
    page.save();
    expect(saved!).toEqual([
      { category_id: 1, amount: '1650.00' },
      { category_id: 9, amount: '2000.00' },
    ]);
  });

  it('takes one figure for one row when it is clicked', () => {
    const page = render(plan()).componentInstance;
    page.use(page.plan()!.rows[0], 'typical');
    expect(page.amounts()[1]).toBe('1600.00');
  });

  it('says when the plan spends more than comes in, and by how much', () => {
    const page = render(plan()).componentInstance;
    page.setAmount(9, '1500');
    expect(page.left()).toBe('-110.00');
    expect(page.short()).toBeTrue();
  });

  it("reckons against last month's income while none is expected, and says so", () => {
    const page = render(plan()).componentInstance;
    page.setAmount(9, '');
    expect(page.incomeAssumed()).toBeTrue();
    expect(page.left()).toBe('390.00');
  });
});
