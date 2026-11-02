import { Component, computed, input, signal } from '@angular/core';

import { UsdPipe } from '../usd';

/**
 * Spending so far, day by day, against last month and against the budget.
 *
 * The question it answers is *when*: a month that ends over budget usually
 * went over on two or three days, and the table cannot say which. Three
 * lines, one axis — this month solid, last month dashed in grey as the
 * reference it is, and the budget as a straight dotted line from nothing on
 * the 1st to the whole of it on the last day.
 *
 * Numbers here are geometry only. Every figure a reader sees — the tooltip,
 * the end labels — is a string exactly as the server sent it.
 */
@Component({
  selector: 'app-pace-chart',
  imports: [UsdPipe],
  template: `
    <div class="legend">
      <span class="key this"><i></i>{{ thisLabel() }}</span>
      <span class="key last"><i></i>{{ lastLabel() }}</span>
      @if (budgeted()) {
        <span class="key budget"><i></i>Budget, spread evenly</span>
      }
    </div>
    <div class="frame">
      <div class="y-axis" aria-hidden="true">
        @for (t of ticks(); track t.value) {
          <span [style.top.%]="t.top">{{ t.label }}</span>
        }
      </div>
      <div
        class="plot"
        tabindex="0"
        role="img"
        [attr.aria-label]="summary()"
        (mousemove)="hover($event)"
        (mouseleave)="day.set(null)"
        (keydown)="key($event)"
        (blur)="day.set(null)"
      >
        <svg viewBox="0 0 1000 1000" preserveAspectRatio="none">
          @for (t of ticks(); track t.value) {
            <line class="grid" x1="0" x2="1000" [attr.y1]="t.top * 10" [attr.y2]="t.top * 10" />
          }
          @if (budgeted()) {
            <line class="budget" x1="0" y1="1000" x2="1000" [attr.y2]="y(num(budgeted()!))" />
          }
          <path class="last" [attr.d]="path(previous())" />
          <path class="this" [attr.d]="path(current())" />
          @if (day() !== null) {
            <line class="cross" [attr.x1]="x(day()!)" [attr.x2]="x(day()!)" y1="0" y2="1000" />
          }
        </svg>
        @if (current().length; as n) {
          <span class="dot" [style.left.%]="x(n - 1) / 10" [style.top.%]="y(num(current()[n - 1])) / 10"></span>
        }
        @if (day() !== null) {
          @let d = day()!;
          <div class="tip" [class.flip]="d > days() / 2" [style.left.%]="x(d) / 10">
            <strong>Day {{ d + 1 }}</strong>
            <span><i class="this"></i>{{ thisLabel() }}
              <b>{{ (current()[d] | usd) ?? 'not yet' }}</b></span>
            <span><i class="last"></i>{{ lastLabel() }}
              <b>{{ (previous()[d] | usd) ?? '—' }}</b></span>
          </div>
        }
      </div>
      <div class="x-axis" aria-hidden="true">
        @for (d of xTicks(); track d) {
          <span [style.left.%]="x(d - 1) / 10">{{ d }}</span>
        }
      </div>
    </div>
  `,
  styleUrl: './pace-chart.scss',
})
export class PaceChart {
  /** Running spending at the end of each day, this month through today. */
  current = input.required<string[]>();
  /** The same for the whole of last month. */
  previous = input.required<string[]>();
  budgeted = input<string | null>(null);
  days = input.required<number>();
  thisLabel = input.required<string>();
  lastLabel = input.required<string>();

  /** The hovered day, zero-based. */
  day = signal<number | null>(null);

  num(value: string): number {
    return Number(value);
  }

  /** The top of the scale: a round figure above every line. */
  private top = computed(() => {
    const values = [...this.current(), ...this.previous()].map(Number);
    if (this.budgeted()) values.push(Number(this.budgeted()));
    const max = Math.max(1, ...values);
    const step = niceStep(max / 4);
    return Math.ceil(max / step) * step;
  });

  ticks = computed(() => {
    const top = this.top();
    const step = niceStep(top / 4);
    const out = [];
    for (let v = 0; v <= top + 1e-9; v += step) {
      out.push({ value: v, top: 100 - (v / top) * 100, label: compact(v) });
    }
    return out;
  });

  /** The 1st, then every 5th day, and the last. */
  xTicks = computed(() => {
    const days = this.days();
    const out = [1];
    for (let d = 5; d < days - 2; d += 5) out.push(d);
    out.push(days);
    return out;
  });

  x(index: number): number {
    const span = Math.max(1, this.days() - 1);
    return (index / span) * 1000;
  }

  y(value: number): number {
    return 1000 - (value / this.top()) * 1000;
  }

  path(series: string[]): string {
    return series
      .map((v, i) => `${i ? 'L' : 'M'}${this.x(i).toFixed(1)},${this.y(Number(v)).toFixed(1)}`)
      .join(' ');
  }

  hover(event: MouseEvent): void {
    const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const share = (event.clientX - box.left) / box.width;
    this.day.set(Math.max(0, Math.min(this.days() - 1, Math.round(share * (this.days() - 1)))));
  }

  key(event: KeyboardEvent): void {
    const last = this.days() - 1;
    const now = this.day() ?? Math.max(0, this.current().length - 1);
    if (event.key === 'ArrowLeft') this.day.set(Math.max(0, now - 1));
    else if (event.key === 'ArrowRight') this.day.set(Math.min(last, now + 1));
    else return;
    event.preventDefault();
  }

  summary = computed(() => {
    const c = this.current();
    const today = c.length ? c[c.length - 1] : '0';
    const then = this.previous()[c.length - 1];
    return (
      `Spending through the month. ${this.thisLabel()}: ${today} by day ${c.length}.` +
      (then ? ` ${this.lastLabel()}: ${then} by the same day.` : '') +
      ' Use the arrow keys to read each day.'
    );
  });
}

/** 1, 2 or 5 times a power of ten — gridlines at figures people round to. */
function niceStep(raw: number): number {
  const power = 10 ** Math.floor(Math.log10(Math.max(raw, 1)));
  const scaled = raw / power;
  return (scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10) * power;
}

/** Axis labels: "$0", "$500", "$1.5K". Round gridline values, not sums. */
function compact(v: number): string {
  if (v >= 1000) return `$${(v / 1000).toFixed(v % 1000 ? 1 : 0)}K`;
  return `$${v}`;
}
