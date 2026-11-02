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
    fixture.componentRef.setInput('groups', [
      { key: 'checking', label: 'Checking', state: 'on' },
      { key: 'credit', label: 'Credit cards', state: 'on' },
      { key: 'savings', label: 'Savings', state: 'off' },
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
    // From zero to the top of the assets; the debt is cut from that, not hung below zero.
    expect(labels).toEqual(['$0', '$100', '$200', '$300', '$400', '$500', '$600']);
    // Three days with no Monday among them: no week to label.
    expect(el.querySelectorAll('.x-axis span').length).toBe(0);
  });

  it('draws a band per group, with debt hatched off the top of the assets', () => {
    const fixture = render();
    const chart = fixture.componentInstance;
    expect(chart.bands().map((b) => [b.group, b.owed])).toEqual([
      ['checking', false],
      ['credit', true],
    ]);
    expect(chart.zero()).toBeNull();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelectorAll('path.band').length).toBe(2);
  });

  it("puts the latest day's figures in the legend, and the hovered day's when hovering", () => {
    const fixture = render();
    const el: HTMLElement = fixture.nativeElement;
    const chips = () =>
      [...el.querySelectorAll('.legend .chip')].map((c) =>
        [...c.querySelectorAll('span')].map((s) => s.textContent!.trim()).join(' '),
      );
    expect(chips()).toEqual(['Checking $600.00', 'Credit cards −$100.00', 'Savings']);

    fixture.componentInstance.hover.set(0);
    fixture.componentRef.setInput('points', [
      { ...point('2026-09-30', '400.00'), groups: [{ group: 'checking', total: '400.00', accounts: [] }] },
      point('2026-10-01', '500.00'),
    ]);
    fixture.detectChanges();
    expect(chips()).toEqual(['Checking $400.00', 'Credit cards', 'Savings']);
  });

  it('lifts the band under the pointer and says which group it is', () => {
    const fixture = render();
    const chart = fixture.componentInstance;
    chart.hover.set(1);
    chart.lit.set('credit');
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('path.band.g-credit')!.classList).toContain('lit');
    expect(el.querySelector('path.band.g-checking')!.classList).toContain('faded');
    expect(el.querySelector('.tooltip .group.lit')!.textContent).toContain('Credit cards');
  });

  it('asks the page to toggle a group when its chip is clicked', () => {
    const fixture = render();
    const toggled: string[] = [];
    fixture.componentInstance.toggle.subscribe((g) => toggled.push(g));
    (fixture.nativeElement.querySelectorAll('.legend .chip')[2] as HTMLElement).click();
    expect(toggled).toEqual(['savings']);
  });

  it('finds the debt slice under the pointer, not the asset beneath it', () => {
    const fixture = render();
    const chart = fixture.componentInstance;
    const plot: HTMLElement = fixture.nativeElement.querySelector('.plot');
    spyOn(plot, 'getBoundingClientRect').and.returnValue(new DOMRect(0, 0, 100, 600));
    // $550 sits in the $500–$600 card slice; $200 is checking.
    chart.move({ currentTarget: plot, clientX: 50, clientY: 50 } as unknown as PointerEvent);
    expect(chart.lit()).toBe('credit');
    chart.move({ currentTarget: plot, clientX: 50, clientY: 400 } as unknown as PointerEvent);
    expect(chart.lit()).toBe('checking');
  });

  it('marks every day when there are few, and labels weeks across a short range', () => {
    const fixture = render();
    expect(fixture.componentInstance.markers().length).toBe(3);
    fixture.componentRef.setInput('points', [
      point('2026-09-27', '400.00'),
      point('2026-09-28', '400.00'),
      point('2026-09-29', '400.00'),
    ]);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    const labels = [...el.querySelectorAll('.x-axis span')].map((s) => s.textContent!.trim());
    expect(labels).toEqual(['Sep 28']);
  });
});
