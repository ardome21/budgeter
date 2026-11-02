import { Injectable, inject, signal } from '@angular/core';

import { Api } from './api';
import { LinkedItem, RefreshResult } from './models';

/**
 * Keeping the ledger current without anyone having to remember to.
 *
 * The first time the app is opened each day it refreshes every bank on its
 * own; the button in the header does the same whenever you like. A refresh
 * writes what comes back — there is no review step, because the one there
 * used to be was never taken and four banks' transactions never landed.
 *
 * Screens that show bank data read `version` and reload when it moves, so a
 * refresh that finishes while you are looking at the Month screen updates it
 * in place rather than leaving yesterday's figures up.
 */
@Injectable({ providedIn: 'root' })
export class Sync {
  private api = inject(Api);

  running = signal(false);
  /** Bumped after every refresh that wrote anything. Zero until the first. */
  version = signal(0);
  /** When the banks were last asked, by this session or an earlier one. */
  lastAt = signal<Date | null>(null);
  failures = signal<string[]>([]);

  private checked = false;

  /** Refresh unless every reachable bank has already been asked today.
   *  Once per session — navigating around does not re-check. */
  daily(): void {
    if (this.checked) return;
    this.checked = true;
    this.api.linkedItems().subscribe((items) => {
      this.lastAt.set(latest(items));
      if (items.some((i) => !i.needs_reauth && !syncedToday(i))) this.run();
    });
  }

  run(): void {
    if (this.running()) return;
    this.running.set(true);
    this.api.refresh().subscribe({
      next: (result: RefreshResult) => {
        this.failures.set(result.failures);
        this.lastAt.set(new Date());
        this.running.set(false);
        this.version.update((v) => v + 1);
      },
      error: () => {
        this.failures.set(['Could not reach the server.']);
        this.running.set(false);
      },
    });
  }
}

/** In local time: a refresh at 11pm and one at 7am are different days. */
function syncedToday(item: LinkedItem): boolean {
  if (!item.last_synced_at) return false;
  return new Date(item.last_synced_at).toDateString() === new Date().toDateString();
}

function latest(items: LinkedItem[]): Date | null {
  const times = items
    .map((i) => i.last_synced_at)
    .filter((t): t is string => !!t)
    .map((t) => new Date(t).getTime());
  return times.length ? new Date(Math.max(...times)) : null;
}
