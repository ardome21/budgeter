import { TestBed } from '@angular/core/testing';

import { PaceChart } from './pace-chart';

describe('PaceChart', () => {
  function render(budgeted: string | null = '900.00') {
    const fixture = TestBed.createComponent(PaceChart);
    fixture.componentRef.setInput('current', ['139.03', '139.03']);
    fixture.componentRef.setInput('previous', ['30.00', '60.00', '1660.00']);
    fixture.componentRef.setInput('budgeted', budgeted);
    fixture.componentRef.setInput('days', 30);
    fixture.componentRef.setInput('thisLabel', 'November');
    fixture.componentRef.setInput('lastLabel', 'October');
    fixture.detectChanges();
    return fixture;
  }

  it('draws this month, last month and the budget on one axis', () => {
    const el = render().nativeElement as HTMLElement;
    expect(el.querySelector('path.this')?.getAttribute('d')).toMatch(/^M0\.0,/);
    expect(el.querySelector('path.last')).toBeTruthy();
    expect(el.querySelector('line.budget')).toBeTruthy();
    // A round top above the highest line, not the highest line itself.
    const labels = Array.from(el.querySelectorAll('.y-axis span')).map((s) => s.textContent);
    expect(labels[labels.length - 1]).toBe('$2K');
  });

  it('leaves the budget line out when there is no budget', () => {
    const el = render(null).nativeElement as HTMLElement;
    expect(el.querySelector('line.budget')).toBeNull();
    expect(el.textContent).not.toContain('Budget, spread evenly');
  });

  it('reads each day with the arrow keys, figures as the server sent them', () => {
    const fixture = render();
    const plot = fixture.nativeElement.querySelector('.plot') as HTMLElement;
    plot.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    fixture.detectChanges();
    const tip = fixture.nativeElement.querySelector('.tip') as HTMLElement;
    expect(tip.textContent).toContain('Day 3');
    expect(tip.textContent).toContain('not yet');
    expect(tip.textContent).toContain('$1,660.00');
  });
});
