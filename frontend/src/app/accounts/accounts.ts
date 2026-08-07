import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api } from '../api';
import { AccountRow, NetWorthPoint, formatMoney, parseDay } from '../models';

/** A point with its plotted geometry attached. */
interface Plotted {
  p: NetWorthPoint;
  x: number;
  yRetirement: number;
  yLiquid: number;
  net: number;
  retirement: number;
  liquid: number;
  /** True when the previous snapshot is far enough back to be a real gap. */
  gapBefore: boolean;
}

// Geometry. A viewBox rather than measured pixels: the chart scales with its
// container and the maths stays in one coordinate system.
const W = 760;
const H = 260;
const PAD = { top: 18, right: 74, bottom: 30, left: 62 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;

// Two snapshots more than four months apart are not a trend, they are a gap.
const GAP_DAYS = 120;

@Component({
  selector: 'app-accounts',
  imports: [FormsModule],
  templateUrl: './accounts.html',
  styleUrl: './accounts.scss',
})
export class Accounts {
  private api = inject(Api);

  accounts = signal<AccountRow[]>([]);
  points = signal<NetWorthPoint[]>([]);
  loading = signal(true);
  saving = signal(false);
  error = signal<string | null>(null);
  hovered = signal<Plotted | null>(null);
  showTable = signal(false);

  // Recording a snapshot: one date, then a balance per account.
  snapshotDate = signal(new Date().toISOString().slice(0, 10));
  draft = signal<Partial<Record<number, string>>>({});
  entryOpen = signal(false);

  readonly fmt = formatMoney;
  readonly W = W;
  readonly H = H;
  readonly PAD = PAD;
  readonly PLOT_W = PLOT_W;
  readonly PLOT_H = PLOT_H;

  latest = computed(() => this.points().at(-1) ?? null);

  /** Counted in the most recent net worth figure. */
  currentAccounts = computed(() =>
    this.accounts().filter((a) => !a.is_stale && !a.closed_on),
  );

  /**
   * Read at some point, but not on the latest snapshot date — so the balance
   * is history. A two-year-old loan shown as "latest" reads as money still
   * owed, which is how a settled debt haunts a net worth screen.
   */
  staleAccounts = computed(() =>
    this.accounts().filter((a) => a.is_stale && !a.closed_on),
  );

  closedAccounts = computed(() => this.accounts().filter((a) => !!a.closed_on));

  /** Accounts to ask for on the next snapshot: everything still open. */
  openAccounts = computed(() => this.accounts().filter((a) => !a.closed_on));

  /** Y scale bounds. Zero is always included — a truncated axis exaggerates. */
  private bounds = computed(() => {
    const values = this.points().flatMap((p) => [
      Number(p.retirement),
      Number(p.liquid),
    ]);
    const max = Math.max(0, ...values);
    const min = Math.min(0, ...values);
    // Round the top out to a clean number so the ticks read well.
    const step = Math.pow(10, Math.floor(Math.log10(max || 1))) / 2;
    return { min, max: Math.ceil((max || 1) / step) * step };
  });

  private xScale = computed(() => {
    const times = this.points().map((p) => parseDay(p.as_of).getTime());
    const lo = Math.min(...times);
    const hi = Math.max(...times);
    // A single point (or several on one day) has no span; centre it.
    return (t: number) =>
      hi === lo
        ? PAD.left + PLOT_W / 2
        : PAD.left + ((t - lo) / (hi - lo)) * PLOT_W;
  });

  private yScale = computed(() => {
    const { min, max } = this.bounds();
    return (v: number) => PAD.top + (1 - (v - min) / (max - min || 1)) * PLOT_H;
  });

  plotted = computed<Plotted[]>(() => {
    const x = this.xScale();
    const y = this.yScale();
    let previous: number | null = null;
    return this.points().map((p) => {
      const t = parseDay(p.as_of).getTime();
      const gapBefore =
        previous !== null && (t - previous) / 86_400_000 > GAP_DAYS;
      previous = t;
      return {
        p,
        x: x(t),
        yRetirement: y(Number(p.retirement)),
        yLiquid: y(Number(p.liquid)),
        net: Number(p.net_worth),
        retirement: Number(p.retirement),
        liquid: Number(p.liquid),
        gapBefore,
      };
    });
  });

  /**
   * One path per segment rather than one per series, so a segment spanning a
   * real gap can be drawn dashed. A solid line across 21 months of nothing
   * would claim readings that were never taken.
   */
  segments = computed(() => {
    const pts = this.plotted();
    const out: { d: string; series: 'retirement' | 'liquid'; gap: boolean }[] =
      [];
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1];
      const b = pts[i];
      out.push({
        d: `M${a.x},${a.yRetirement} L${b.x},${b.yRetirement}`,
        series: 'retirement',
        gap: b.gapBefore,
      });
      out.push({
        d: `M${a.x},${a.yLiquid} L${b.x},${b.yLiquid}`,
        series: 'liquid',
        gap: b.gapBefore,
      });
    }
    return out;
  });

  gridLines = computed(() => {
    const { min, max } = this.bounds();
    const y = this.yScale();
    const ticks = 4;
    return Array.from({ length: ticks + 1 }, (_, i) => {
      const value = min + ((max - min) / ticks) * i;
      return { value, y: y(value), label: this.axisLabel(value) };
    });
  });

  /**
   * Selective x labels.
   *
   * Time-proportional spacing is honest — seven snapshots really did happen in
   * one nine-month stretch — but it crowds them into the left quarter, where
   * labelling every point overlaps into mush. Keep the first and last, and in
   * between only those with room to stand.
   */
  xTicks = computed(() => {
    const pts = this.plotted();
    const MIN_SPACING = 52;
    const out: { x: number; label: string }[] = [];
    let lastX = -Infinity;
    pts.forEach((pt, i) => {
      const isLast = i === pts.length - 1;
      if (!isLast && pt.x - lastX < MIN_SPACING) return;
      // Never let the final label be pushed onto a neighbour.
      if (isLast && out.length && pt.x - out[out.length - 1].x < MIN_SPACING) {
        out.pop();
      }
      lastX = pt.x;
      out.push({
        x: pt.x,
        label: parseDay(pt.p.as_of).toLocaleDateString(undefined, {
          month: 'short',
          year: '2-digit',
        }),
      });
    });
    return out;
  });

  /** Hit bands, so a hover target is the width of a slot, not of a dot. */
  bands = computed(() => {
    const pts = this.plotted();
    return pts.map((pt, i) => {
      const before = i === 0 ? pt.x : (pts[i - 1].x + pt.x) / 2;
      const after = i === pts.length - 1 ? pt.x : (pt.x + pts[i + 1].x) / 2;
      return {
        pt,
        x: i === 0 ? PAD.left : before,
        width: Math.max(
          (i === pts.length - 1 ? PAD.left + PLOT_W : after) -
            (i === 0 ? PAD.left : before),
          1,
        ),
      };
    });
  });

  constructor() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.api.netWorth().subscribe({
      next: (nw) => {
        this.points.set(nw.points);
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.loading.set(false);
      },
    });
    this.api.accounts().subscribe({
      next: (rows) => this.accounts.set(rows),
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  axisLabel(value: number): string {
    const abs = Math.abs(value);
    if (abs >= 1000)
      return `${value < 0 ? '−' : ''}$${Math.round(abs / 1000)}k`;
    return `${value < 0 ? '−' : ''}$${Math.round(abs)}`;
  }

  longDate(iso: string): string {
    return parseDay(iso).toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    });
  }

  monthsBehind(days: number | null): string {
    if (!days) return '';
    const months = Math.round(days / 30.44);
    return months >= 12
      ? `${(months / 12).toFixed(months % 12 === 0 ? 0 : 1)} years behind`
      : `${months} months behind`;
  }

  /** Settle an account, or reopen one. History is untouched either way. */
  toggleClosed(a: AccountRow): void {
    const closing = !a.closed_on;
    if (
      closing &&
      !confirm(
        `Mark ${a.institution} / ${a.name} as closed?\n\n` +
          'Its balance history stays and net worth is unchanged — it just ' +
          'stops being shown as a current position, and stops being asked ' +
          'for on the next snapshot.',
      )
    ) {
      return;
    }
    this.api
      .closeAccount(
        a.id,
        closing ? new Date().toISOString().slice(0, 10) : null,
      )
      .subscribe({
        next: () => this.load(),
        error: (e) => this.error.set(this.describe(e)),
      });
  }

  setDraft(accountId: number, value: string): void {
    this.draft.update((d) => ({ ...d, [accountId]: value }));
  }

  /** Record every filled-in balance under one date. */
  saveSnapshot(): void {
    // Narrow while filtering: a Partial record yields `string | undefined`.
    const entries = Object.entries(this.draft()).filter(
      (e): e is [string, string] => !!e[1] && e[1].trim() !== '',
    );
    if (!entries.length) {
      this.error.set('Enter at least one balance.');
      return;
    }
    this.saving.set(true);
    this.error.set(null);

    let remaining = entries.length;
    let failed = false;
    for (const [id, value] of entries) {
      this.api
        .recordBalance(Number(id), this.snapshotDate(), value.trim())
        .subscribe({
          next: () => {
            if (--remaining === 0) this.finishSnapshot(failed);
          },
          error: (e) => {
            failed = true;
            this.error.set(this.describe(e));
            if (--remaining === 0) this.finishSnapshot(failed);
          },
        });
    }
  }

  private finishSnapshot(failed: boolean): void {
    this.saving.set(false);
    if (!failed) {
      this.draft.set({});
      this.entryOpen.set(false);
    }
    this.load();
  }

  private describe(e: unknown): string {
    const err = e as { status?: number; error?: { detail?: string } };
    if (err?.status === 0)
      return 'Cannot reach the API. Is the backend running?';
    return err?.error?.detail ?? 'Something went wrong.';
  }
}
