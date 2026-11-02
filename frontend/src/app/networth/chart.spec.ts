import { TestBed } from '@angular/core/testing';

import { NetWorthPoint } from '../models';
import { NetWorthChart } from './chart';

function point(as_of: string, net: string, estimated = false): NetWorthPoint {
  return {
    as_of,
    net,
    assets: net,
    liabilities: '0.00',
    accounts_reporting: 2,
    estimated,
    groups: [
      {
        group: 'credit',
        total: '-100.00',
        accounts: [{ account_id: 2, name: 'Venture', amount: '-100.00', source: 'rebuilt' }],
      },
      {
        group: 'checking',
        total: '600.00',
        accounts: [{ account_id: 1, name: 'Checking', amount: '600.00', source: 'bank' }],
      },
    ],
  };
}

describe('NetWorthChart', () => {
  function render() {
    const fixture = TestBed.createComponent(NetWorthChart);
    fixture.componentRef.setInput('points', [
      point('2026-09-30', '400.00'),
      point('2026-10-01', '500.00', true),
      point('2026-10-02', '450.00'),
    ]);
    fixture.componentRef.setInput('groupLabels', [
      { key: 'checking', label: 'Checking' },
      { key: 'credit', label: 'Credit cards' },
    ]);
    fixture.detectChanges();
    return fixture;
  }

  it('shows nothing until a day is hovered', () => {
    const el: HTMLElement = render().nativeElement;
    expect(el.querySelector('.tooltip')).toBeNull();
  });

  it("shows the hovered day's figures, grouped in the page's order", () => {
    const fixture = render();
    fixture.componentInstance.hover.set(1);
    fixture.detectChanges();

    const tip: HTMLElement = fixture.nativeElement.querySelector('.tooltip');
    expect(tip.querySelector('.net')!.textContent!.trim()).toBe('$500.00');
    expect(tip.querySelector('.tag')!.textContent).toContain('rebuilt');
    const heads = [...tip.querySelectorAll('.row.head')].map((r) =>
      [...r.querySelectorAll('span')].map((s) => s.textContent!.trim()).join(' '),
    );
    expect(heads).toEqual(['Checking $600.00', 'Credit cards −$100.00']);
    expect(tip.querySelector('.est')).not.toBeNull();
  });

  it('walks between days with the arrow keys', () => {
    const fixture = render();
    const chart = fixture.componentInstance;
    chart.focus();
    expect(chart.hover()).toBe(2);
    chart.key(new KeyboardEvent('keydown', { key: 'ArrowLeft' }));
    chart.key(new KeyboardEvent('keydown', { key: 'ArrowLeft' }));
    chart.key(new KeyboardEvent('keydown', { key: 'ArrowLeft' }));
    expect(chart.hover()).toBe(0);
  });

  it('labels the axis in round compact steps', () => {
    const el: HTMLElement = render().nativeElement;
    const labels = [...el.querySelectorAll('.y-axis span')].map((s) => s.textContent!.trim());
    expect(labels).toEqual(['$400', '$425', '$450', '$475', '$500']);
    const months = [...el.querySelectorAll('.x-axis span')].map((s) => s.textContent!.trim());
    expect(months).toEqual(['Oct']);
  });
});
