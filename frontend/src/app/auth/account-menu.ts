import { Component, ElementRef, HostListener, inject, signal } from '@angular/core';

import { PasskeyRow } from '../models';
import { Auth } from './auth';

/**
 * Your name in the header, and what is yours behind it: the devices that can
 * sign you in, and signing out.
 *
 * Adding a passkey was built — `Auth.registerPasskey` — and never reachable:
 * the setup flow did not offer it, and nothing after setup did either. So the
 * Touch ID button on the login screen could only ever appear on a machine
 * nobody had a way to register.
 */
@Component({
  selector: 'app-account-menu',
  template: `
    <button type="button" class="trigger" (click)="toggle()" [attr.aria-expanded]="open()"
            aria-haspopup="true">
      {{ auth.username() }} <span aria-hidden="true">▾</span>
    </button>
    @if (open()) {
      <div class="panel" role="dialog" aria-label="Your account">
        <h3>Sign-in devices</h3>
        @if (passkeys().length) {
          <ul>
            @for (p of passkeys(); track p.id) {
              <li>
                <span class="label">{{ p.label }}</span>
                <span class="dim">
                  added {{ day(p.created_at) }}
                  @if (p.last_used_at) { · used {{ day(p.last_used_at) }} }
                </span>
                <button type="button" class="link-button" (click)="remove(p)">Remove</button>
              </li>
            }
          </ul>
        } @else {
          <p class="dim">None yet. Add this one to sign in with Touch ID.</p>
        }
        <div class="add">
          <input type="text" [value]="label()" (input)="label.set($any($event.target).value)"
                 aria-label="Name for this device" />
          <button type="button" (click)="add()" [disabled]="adding() || !label().trim()">
            {{ adding() ? 'Waiting…' : 'Add this device' }}
          </button>
        </div>
        @if (error()) {
          <p class="error">{{ error() }}</p>
        }
        <hr />
        <button type="button" class="signout" (click)="auth.logout()">Sign out</button>
      </div>
    }
  `,
  styles: `
    :host { position: relative; flex-shrink: 0; }
    .trigger {
      background: none; border: 1px solid transparent; color: var(--text-dim);
      font-size: 13px; padding: 0.3rem 0.6rem; border-radius: 7px; cursor: pointer;
      &:hover, &[aria-expanded='true'] { border-color: var(--border); color: var(--text); }
    }
    .panel {
      position: absolute; right: 0; top: calc(100% + 6px); width: 21rem;
      padding: 0.9rem 1rem; background: var(--surface);
      border: 1px solid var(--border); border-radius: var(--radius);
      box-shadow: 0 8px 24px rgb(0 0 0 / 0.12); font-size: 13.5px; z-index: 20;
    }
    h3 { margin: 0 0 0.5rem; font-size: 13px; font-weight: 600; }
    ul { list-style: none; margin: 0 0 0.6rem; padding: 0; }
    li {
      display: grid; grid-template-columns: 1fr auto; gap: 0 0.6rem;
      padding: 0.35rem 0; border-bottom: 1px solid var(--border);
      .dim { grid-column: 1; font-size: 12px; }
      button { grid-column: 2; grid-row: 1 / span 2; align-self: center; }
    }
    .dim { color: var(--text-dim); }
    p { margin: 0 0 0.6rem; }
    .add { display: flex; gap: 0.4rem; input { flex: 1; min-width: 0; } }
    .error { color: var(--over); margin-top: 0.5rem; }
    hr { border: 0; border-top: 1px solid var(--border); margin: 0.8rem 0 0.6rem; }
    .signout {
      width: 100%; background: none; border: 1px solid var(--border); color: var(--text);
    }
    .link-button {
      all: unset; cursor: pointer; font-size: 12.5px; color: var(--text-dim);
      border-bottom: 1px solid var(--border);
      &:hover { color: var(--over); border-bottom-color: var(--over); }
    }
  `,
})
export class AccountMenu {
  auth = inject(Auth);
  private host = inject(ElementRef<HTMLElement>);

  open = signal(false);
  passkeys = signal<PasskeyRow[]>([]);
  label = signal(defaultLabel());
  adding = signal(false);
  error = signal<string | null>(null);

  toggle(): void {
    this.open.update((o) => !o);
    if (this.open()) this.load();
  }

  @HostListener('document:click', ['$event'])
  outside(event: MouseEvent): void {
    if (this.open() && !this.host.nativeElement.contains(event.target as Node)) {
      this.open.set(false);
    }
  }

  @HostListener('document:keydown.escape')
  escape(): void {
    this.open.set(false);
  }

  async add(): Promise<void> {
    this.adding.set(true);
    this.error.set(null);
    try {
      await this.auth.registerPasskey(this.label().trim());
      this.load();
    } catch (e) {
      // Cancelling the Touch ID prompt is a choice, not an error.
      const name = (e as Error)?.name;
      if (name !== 'NotAllowedError' && (e as Error)?.message !== 'cancelled') {
        this.error.set(this.auth.describe(e));
      }
    } finally {
      this.adding.set(false);
    }
  }

  remove(passkey: PasskeyRow): void {
    if (!confirm(`Remove ${passkey.label}? It will no longer sign you in.`)) return;
    this.auth.removePasskey(passkey.id).subscribe({
      next: () => {
        this.load();
        this.auth.refresh().subscribe();
      },
      error: (e) => this.error.set(this.auth.describe(e)),
    });
  }

  day(iso: string): string {
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  private load(): void {
    this.auth.passkeys().subscribe({
      next: (rows) => this.passkeys.set(rows),
      error: (e) => this.error.set(this.auth.describe(e)),
    });
  }
}

/** "This Mac", "This iPhone" — a name worth recognising in the list later. */
function defaultLabel(): string {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad/.test(ua)) return 'iPad';
  if (/Mac/.test(ua)) return 'Mac';
  return 'This device';
}
