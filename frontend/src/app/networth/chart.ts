import { Component, computed, input, signal } from '@angular/core';

import { AccountGroup, NetWorthPoint } from '../models';
import { UsdPipe } from '../usd';

interface Tick {
  label: string;
  /** Percent of the plot, from the top (y) or the left (x). */
  at: number;
}

/**
 * Net worth over time, and what any one day is made of.
 *
 * Inline SVG for the gridlines and the line, scaled to fill the width; the
 * text, the hover dot and the tooltip are HTML laid over it by percentage, so
 * none of them stretch with the plot. No chart library: this is one series.
 *
 * The crosshair snaps to the nearest day, so nobody has to land on a 2px line,
 * and the arrow keys walk it when the chart has focus. Everything the tooltip
 * shows was summed by the server — the only arithmetic here is geometry, done
 * on `Number()` copies that never reach the screen as money.
 */
@Component({
  selector: 'app-networth-chart',
  imports: [UsdPipe],
  templateUrl: './chart.html',
  styleUrl: './chart.scss',
})
export class NetWorthChart {
  points = input.required<NetWorthPoint[]>();
  /** Display names and order for the groups, from the page's filter chips. */
  groupLabels = input.required<{ key: AccountGroup; label: string }[]>();

  readonly width = 1000;
  readonly height = 240;

  /** Index of the day under the crosshair, or null when nothing is hovered. */
  hover = signal<number | null>(null);

  private values = computed(() => this.points().map((p) => Number(p.net)));

  /** Four or five round steps covering the data, so ticks read $170K, $175K. */
  private domain = computed(() => {
    const values = this.values();
    const ticks = niceTicks(Math.min(...values), Math.max(...values));
    return { ticks, low: ticks[0], high: ticks[ticks.length - 1] };
  });

  private x(i: number): number {
    const n = this.points().length;
    return n < 2 ? 0 : (i / (n - 1)) * 100;
  }

  private y(value: number): number {
    const { low, high } = this.domain();
    return 100 - ((value - low) / (high - low || 1)) * 100;
  }

  path = computed(() =>
    this.values()
      .map((v, i) => {
        const px = (this.x(i) / 100) * this.width;
        const py = (this.y(v) / 100) * this.height;
        return `${i === 0 ? 'M' : 'L'}${px.toFixed(1)},${py.toFixed(1)}`;
      })
      .join(' '),
  );

  yTicks = computed<Tick[]>(() =>
    this.domain().ticks.map((v) => ({ label: compact(v), at: this.y(v) })),
  );

  /** One tick per month, on the first day of it the series contains. */
  xTicks = computed<Tick[]>(() => {
    const ticks: Tick[] = [];
    let month = '';
    this.points().forEach((p, i) => {
      const key = p.as_of.slice(0, 7);
      if (key === month) return;
      month = key;
      if (i === 0 && p.as_of.slice(8) !== '01') return; // a part-month label crowds the next
      const [y, m] = key.split('-').map(Number);
      ticks.push({
        label: new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'short' }),
        at: this.x(i),
      });
    });
    return ticks;
  });

  current = computed(() => {
    const i = this.hover();
    if (i === null) return null;
    const point = this.points()[i];
    const labels = this.groupLabels();
    const groups = [...point.groups].sort(
      (a, b) =>
        labels.findIndex((l) => l.key === a.group) - labels.findIndex((l) => l.key === b.group),
    );
    return {
      point,
      groups: groups.map((g) => ({
        ...g,
        label: labels.find((l) => l.key === g.group)?.label ?? g.group,
      })),
      left: this.x(i),
      top: this.y(this.values()[i]),
      // Past the middle the tooltip opens leftwards, so it never runs off the edge.
      flip: this.x(i) > 50,
    };
  });

  move(event: PointerEvent): void {
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    this.hover.set(Math.round(fraction * (this.points().length - 1)));
  }

  key(event: KeyboardEvent): void {
    const last = this.points().length - 1;
    const i = this.hover() ?? last;
    const next =
      event.key === 'ArrowLeft' ? i - 1
      : event.key === 'ArrowRight' ? i + 1
      : event.key === 'Home' ? 0
      : event.key === 'End' ? last
      : null;
    if (next === null) return;
    event.preventDefault();
    this.hover.set(Math.min(last, Math.max(0, next)));
  }

  focus(): void {
    if (this.hover() === null) this.hover.set(this.points().length - 1);
  }

  day(iso: string): string {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  }
}

/** Round steps (1, 2, 2.5 or 5 × a power of ten) covering [min, max]. */
function niceTicks(min: number, max: number, target = 4): number[] {
  if (min === max) {
    const pad = Math.abs(min) * 0.05 || 1;
    min -= pad;
    max += pad;
  }
  const rough = (max - min) / target;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => s >= rough)!;
  const ticks: number[] = [];
  for (let v = Math.floor(min / step) * step; v <= max + step / 2; v += step) {
    ticks.push(Number(v.toFixed(10)));
    if (v >= max) break;
  }
  return ticks;
}

/** $175K, $1.2M, −$950. Axis labels only — never a figure anyone reads as exact. */
function compact(v: number): string {
  const sign = v < 0 ? '−' : '';
  const a = Math.abs(v);
  const body =
    a >= 1e6 ? `${trim(a / 1e6)}M` : a >= 1e3 ? `${trim(a / 1e3)}K` : `${Math.round(a)}`;
  return `${sign}$${body}`;
}

function trim(v: number): string {
  return v.toFixed(1).replace(/\.0$/, '');
}
