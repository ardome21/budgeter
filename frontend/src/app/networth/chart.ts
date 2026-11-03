import { Component, ElementRef, computed, inject, input, linkedSignal, output, signal } from '@angular/core';

import { AccountGroup, NetWorthPoint } from '../models';
import { change } from '../money';
import { UsdPipe } from '../usd';

/** A group as the legend shows it: its name, and how much of it is selected. */
export interface LegendGroup {
  key: AccountGroup;
  label: string;
  state: 'on' | 'some' | 'off';
}

interface Band {
  group: AccountGroup;
  path: string;
  /** Debt: drawn hatched, as a slice taken off the top of the assets. */
  owed: boolean;
}

/**
 * Assets bottom to top, so the everyday accounts sit on the baseline. Then
 * the debts, cut downward from the top of the assets, card debt first.
 * Each group keeps its colour (chart.scss) whatever else is selected.
 */
const STACK: AccountGroup[] = [
  'checking',
  'savings',
  'retirement',
  'investment',
  'other',
  'credit',
  'loan',
];

/** The tooltip at its widest (19rem) plus its gap from the edge it hangs off. */
const CARD_ROOM = 320;

/** Beyond this many days the per-day markers would merge into a smear. */
const MARKERS_UP_TO = 62;

interface Tick {
  label: string;
  /** Percent of the plot, from the top (y) or the left (x). */
  at: number;
}

/**
 * Net worth over time, and what any one day is made of.
 *
 * Under the line, the day is split into one band per group: assets stacked up
 * from zero, and debts as a hatched slice cut off the top of them. The line
 * runs along the bottom of that slice, because that is what net worth is.
 * Stacking debt below zero instead put −$50K on the axis to make room for
 * $1.1K, and the debt itself disappeared into a 2px sliver. The legend beneath is also the filter:
 * clicking a group takes it in or out, hovering one lifts its band.
 *
 * Inline SVG for the gridlines and the line, scaled to fill the width; the
 * text, the hover dot and the tooltip are HTML laid over it by percentage, so
 * none of them stretch with the plot. No chart library: this is one series.
 *
 * The crosshair snaps to the nearest day, so nobody has to land on a 2px line,
 * and the arrow keys walk it when the chart has focus. Everything the tooltip
 * shows was summed by the server — the only arithmetic here is geometry, done
 * on `Number()` copies that never reach the screen as money.
 *
 * Dragging across the plot (or Shift + arrows) picks a span, and the tooltip
 * then speaks for the change across it instead of for one day: net, and what
 * each group added or took away. Those are the one sum made here, and they
 * are made exactly, from the server's two totals (`change` in money.ts). A
 * click, Escape, or a new range clears it. The card never sits on the span:
 * it hangs off whichever side has room, and floats just under the plot,
 * centred on the span, when the span leaves room on neither.
 */
@Component({
  selector: 'app-networth-chart',
  imports: [UsdPipe],
  templateUrl: './chart.html',
  styleUrl: './chart.scss',
})
export class NetWorthChart {
  points = input.required<NetWorthPoint[]>();
  /** Every group that has accounts, in the page's order, with its selection. */
  groups = input.required<LegendGroup[]>();
  /** A legend chip was clicked. */
  toggle = output<AccountGroup>();

  readonly width = 1000;
  readonly height = 300;

  /** Index of the day under the crosshair, or null when nothing is hovered. */
  hover = signal<number | null>(null);
  /** The group being pointed at, in the plot or the legend; the rest recede. */
  lit = signal<AccountGroup | null>(null);

  /**
   * The span being compared, as day indices in the order they were picked:
   * `from` is where the drag began. Forgotten whenever the days change,
   * since an index into another range is another day.
   */
  pick = linkedSignal<NetWorthPoint[], { from: number; to: number } | null>({
    source: this.points,
    computation: () => null,
  });
  private dragging = false;
  /** The plot's width in pixels, read when a span is picked: placing the card needs it. */
  private plotWidth = signal(0);
  private host = inject<ElementRef<HTMLElement>>(ElementRef);

  private values = computed(() => this.points().map((p) => Number(p.net)));

  /**
   * Each group's band edges, per day, in the units of the axis. Everything
   * positive stacks up from zero; everything negative is then cut down from
   * the top of that, so the last edge lands on net. Done by sign, not by
   * group, so a card in credit after a refund adds rather than subtracts.
   * Debts are listed first so a pointer over the slice finds the debt, not
   * the asset underneath it.
   */
  private stacks = computed(() =>
    this.points().map((p) => {
      const values = STACK.map((group) => {
        const total = p.groups.find((g) => g.group === group)?.total;
        return { group, v: total === undefined ? 0 : Number(total) };
      });
      const edges = new Map<AccountGroup, [number, number]>();
      let top = 0;
      const assets: [AccountGroup, [number, number]][] = [];
      for (const { group, v } of values) {
        if (v < 0) continue;
        assets.push([group, [top, top + v]]);
        top += v;
      }
      const peak = top;
      for (const { group, v } of values) {
        if (v >= 0) continue;
        edges.set(group, [top + v, top]);
        top += v;
      }
      for (const [group, edge] of assets) edges.set(group, edge);
      return { peak, floor: Math.min(0, top), edges };
    }),
  );

  /** Five to seven round steps covering the stack and the line. */
  private domain = computed(() => {
    const stacks = this.stacks();
    const ticks = niceTicks(
      Math.min(0, ...stacks.map((s) => s.floor)),
      Math.max(0, ...stacks.map((s) => s.peak)),
      6,
    );
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

  private px(i: number): string {
    return ((this.x(i) / 100) * this.width).toFixed(1);
  }

  private py(value: number): string {
    return ((this.y(value) / 100) * this.height).toFixed(1);
  }

  path = computed(() =>
    this.values()
      .map((v, i) => `${i === 0 ? 'M' : 'L'}${this.px(i)},${this.py(v)}`)
      .join(' '),
  );

  /** One closed shape per group present in the series: along its top edge, back along its bottom. */
  bands = computed<Band[]>(() => {
    const stacks = this.stacks();
    const present = new Set(this.points().flatMap((p) => p.groups.map((g) => g.group)));
    return STACK.filter((g) => present.has(g)).map((group) => {
      const top = stacks.map((s, i) => `${this.px(i)},${this.py(s.edges.get(group)![1])}`);
      const bottom = stacks
        .map((s, i) => `${this.px(i)},${this.py(s.edges.get(group)![0])}`)
        .reverse();
      return {
        group,
        path: `M${top.join(' L')} L${bottom.join(' L')} Z`,
        owed: group === 'credit' || group === 'loan',
      };
    });
  });

  /** One dot per day when there are few enough to tell apart. */
  markers = computed(() => {
    const values = this.values();
    if (values.length > MARKERS_UP_TO) return [];
    return values.map((v, i) => ({ left: this.x(i), top: this.y(v) }));
  });

  /** Where the zero line falls, when debts put it inside the plot. */
  zero = computed(() => {
    const { low, high } = this.domain();
    return low < 0 && high > 0 ? (this.y(0) / 100) * this.height : null;
  });

  /** The day the legend speaks for: the hovered one, else the latest. */
  private shown = computed(() => {
    const points = this.points();
    return points[this.hover() ?? points.length - 1];
  });

  /** Legend chips, each with its group's total on the shown day. */
  legend = computed(() => {
    const point = this.shown();
    return this.groups().map((g) => ({
      ...g,
      total: point?.groups.find((d) => d.group === g.key)?.total ?? null,
    }));
  });

  yTicks = computed<Tick[]>(() =>
    this.domain().ticks.map((v) => ({ label: compact(v), at: this.y(v) })),
  );

  /**
   * Labelled ticks along the bottom, each with a vertical gridline: weekly
   * (on Mondays) across a short range, monthly across a long one. Over a
   * medium range the weeks still get faint unlabelled lines, so a day can be
   * placed without counting.
   */
  xTicks = computed<Tick[]>(() => {
    const points = this.points();
    const weekly = points.length <= 45;
    const ticks: Tick[] = [];
    let month = '';
    points.forEach((p, i) => {
      if (weekly) {
        if (local(p.as_of).getDay() === 1) ticks.push({ label: short(p.as_of), at: this.x(i) });
        return;
      }
      const key = p.as_of.slice(0, 7);
      if (key === month) return;
      month = key;
      if (i === 0 && p.as_of.slice(8) !== '01') return; // a part-month label crowds the next
      ticks.push({
        label: local(p.as_of).toLocaleDateString(undefined, { month: 'short' }),
        at: this.x(i),
      });
    });
    return ticks;
  });

  /** Unlabelled weekly lines between the month ticks, up to about half a year. */
  weekLines = computed<number[]>(() => {
    const points = this.points();
    if (points.length <= 45 || points.length > 200) return [];
    return points.flatMap((p, i) => (local(p.as_of).getDay() === 1 ? [this.x(i)] : []));
  });

  current = computed(() => {
    const i = this.hover();
    if (i === null) return null;
    const point = this.points()[i];
    const labels = this.groups();
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

  /**
   * The change from the earlier picked day to the later, or null with no span.
   * Every figure is `later − earlier` of two server totals, so the groups add
   * up to the net. A group absent on one side counts as zero there.
   */
  span = computed(() => {
    const pick = this.pick();
    if (!pick || pick.from === pick.to) return null;
    const [a, b] = pick.from < pick.to ? [pick.from, pick.to] : [pick.to, pick.from];
    const start = this.points()[a];
    const end = this.points()[b];
    const total = (p: NetWorthPoint, g: AccountGroup) =>
      p.groups.find((d) => d.group === g)?.total ?? '0';
    const groups = this.groups()
      .filter((l) => [start, end].some((p) => p.groups.some((d) => d.group === l.key)))
      .map((l) => ({ group: l.key, label: l.label, change: change(total(end, l.key), total(start, l.key)) }));
    const from = Number(start.net);
    const net = change(end.net, start.net);
    const left = this.x(a);
    const right = this.x(b);
    return {
      start,
      end,
      days: Math.round((local(end.as_of).getTime() - local(start.as_of).getTime()) / 86_400_000),
      net,
      // A ratio, not money, so a float is fine. Against a start at or below
      // zero a percentage says nothing true, so there isn't one.
      percent: from > 0 ? percent(Number(net) / from) : null,
      assets: change(end.assets, start.assets),
      liabilities: change(end.liabilities, start.liabilities),
      groups,
      left,
      right,
      ends: [a, b].map((i) => ({ left: this.x(i), top: this.y(this.values()[i]) })),
      ...this.place(left, right),
    };
  });

  /**
   * Where the span's card goes: off the right edge, else off the left, else
   * under the plot, centred on the span but kept inside the plot's width.
   * Never on the span, and always on screen while it is picked.
   */
  private place(left: number, right: number): { side: 'right' | 'left' | 'below'; at: number } {
    const width = this.plotWidth();
    if (((100 - right) / 100) * width >= CARD_ROOM) return { side: 'right', at: right };
    if ((left / 100) * width >= CARD_ROOM) return { side: 'left', at: left };
    const half = width ? (CARD_ROOM / 2 / width) * 100 : 50;
    const middle = (left + right) / 2;
    return { side: 'below', at: Math.min(Math.max(middle, half), 100 - half) };
  }

  private measure(): void {
    this.plotWidth.set(this.host.nativeElement.querySelector('.plot')?.clientWidth ?? 0);
  }

  move(event: PointerEvent): void {
    const i = this.dayAt(event);
    this.hover.set(i);
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    this.lit.set(this.bandAt(i, ((event.clientY - rect.top) / rect.height) * 100));
    if (this.dragging) this.pick.update((p) => p && { ...p, to: i });
  }

  /** Starts a span at the day under the pointer. Held, so the drag survives leaving the plot. */
  press(event: PointerEvent): void {
    if (event.button !== 0) return;
    const i = this.dayAt(event);
    this.dragging = true;
    this.measure();
    this.pick.set({ from: i, to: i });
    (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
  }

  /** Ends the drag. One day picked is a click, and a click clears. */
  release(): void {
    if (!this.dragging) return;
    this.dragging = false;
    const pick = this.pick();
    if (pick && pick.from === pick.to) this.pick.set(null);
  }

  private dayAt(event: PointerEvent): number {
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    return Math.round(fraction * (this.points().length - 1));
  }

  leave(): void {
    this.hover.set(null);
    this.lit.set(null);
  }

  /** The group whose band covers height `at` (percent from the top) on day `i`. */
  private bandAt(i: number, at: number): AccountGroup | null {
    for (const [group, [low, high]] of this.stacks()[i].edges) {
      if (high > low && at <= this.y(low) && at >= this.y(high)) return group;
    }
    return null;
  }

  key(event: KeyboardEvent): void {
    if (event.key === 'Escape' && this.pick()) {
      this.pick.set(null);
      return;
    }
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
    const to = Math.min(last, Math.max(0, next));
    // Shift stretches a span from where the crosshair was; a plain step leaves any span be.
    if (event.shiftKey) this.measure();
    if (event.shiftKey) this.pick.update((p) => ({ from: p?.from ?? i, to }));
    this.hover.set(to);
  }

  focus(): void {
    if (this.hover() === null) this.hover.set(this.points().length - 1);
  }

  short(iso: string): string {
    return short(iso);
  }

  day(iso: string): string {
    return local(iso).toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  }
}

/** A calendar date, parsed as local: `new Date(iso)` reads it as UTC and lands on the day before. */
function local(iso: string): Date {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d);
}

/** Oct 5. */
function short(iso: string): string {
  return local(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
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

/** 0.0234 → "+2.3%". */
function percent(ratio: number): string {
  const sign = ratio > 0 ? '+' : ratio < 0 ? '−' : '';
  return `${sign}${Math.abs(ratio * 100).toFixed(1)}%`;
}

function trim(v: number): string {
  return v.toFixed(1).replace(/\.0$/, '');
}
