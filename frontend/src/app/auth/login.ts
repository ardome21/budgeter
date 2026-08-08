import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';

import { Auth } from './auth';
import { platformAuthenticatorAvailable } from './webauthn';

@Component({
  selector: 'app-login',
  imports: [FormsModule],
  template: `
    <div class="gate">
      <div class="card">
        <h1>budgeter</h1>
        <p class="dim small">Sign in to see your accounts.</p>

        @if (error(); as e) {
          <div class="banner error">{{ e }}</div>
        }

        @if (canUsePasskey()) {
          <button
            class="primary passkey"
            type="button"
            (click)="withPasskey()"
            [disabled]="busy()"
          >
            {{ busy() ? 'Waiting…' : 'Sign in with Touch ID' }}
          </button>
          <div class="or"><span>or</span></div>
        }

        <form (ngSubmit)="submit()">
          <label for="u">Name</label>
          <input
            id="u"
            name="username"
            autocomplete="username"
            [ngModel]="username()"
            (ngModelChange)="username.set($event)"
            required
          />

          <label for="p">Password</label>
          <input
            id="p"
            name="password"
            type="password"
            autocomplete="current-password"
            [ngModel]="password()"
            (ngModelChange)="password.set($event)"
            required
          />

          <label for="c">Six-digit code</label>
          <input
            id="c"
            name="code"
            inputmode="text"
            autocomplete="one-time-code"
            placeholder="123456, or a recovery code"
            [ngModel]="code()"
            (ngModelChange)="code.set($event)"
            required
          />

          <button class="primary" type="submit" [disabled]="busy()">
            {{ busy() ? 'Checking…' : 'Sign in' }}
          </button>
        </form>

        <p class="dim small foot">
          Lost the authenticator and the recovery codes? Run
          <code>uv run python scripts/reset_auth.py --apply</code> in the
          backend directory. It clears the login and touches no data.
        </p>
      </div>
    </div>
  `,
  styleUrl: './gate.scss',
})
export class Login {
  private auth = inject(Auth);
  private router = inject(Router);
  private route = inject(ActivatedRoute);

  username = signal('');
  password = signal('');
  code = signal('');
  busy = signal(false);
  error = signal<string | null>(null);

  /** Only shown when this device actually has a built-in authenticator and a
   *  passkey is registered here. A button that can only disappoint is worse
   *  than no button. */
  canUsePasskey = signal(false);

  constructor() {
    // Both have to be true: a passkey registered here, and a device that can
    // actually use one. Either alone gives a button that only disappoints.
    this.auth.refresh().subscribe({
      next: async (status) => {
        this.canUsePasskey.set(
          status.has_passkeys && (await platformAuthenticatorAvailable()),
        );
      },
      error: () => this.canUsePasskey.set(false),
    });
  }

  async withPasskey(): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      await this.auth.loginWithPasskey();
      const next = this.route.snapshot.queryParamMap.get('next');
      this.router.navigateByUrl(next || '/month');
    } catch (e) {
      // A cancelled Touch ID prompt is not an error worth shouting about.
      const name = (e as { name?: string })?.name;
      if (name !== 'NotAllowedError' && (e as Error)?.message !== 'cancelled') {
        this.error.set(this.auth.describe(e));
      }
    } finally {
      this.busy.set(false);
    }
  }

  submit(): void {
    if (!this.username() || !this.password() || !this.code()) {
      this.error.set('All three are needed.');
      return;
    }
    this.busy.set(true);
    this.error.set(null);
    this.auth.login(this.username(), this.password(), this.code()).subscribe({
      next: () => {
        this.busy.set(false);
        const next = this.route.snapshot.queryParamMap.get('next');
        this.router.navigateByUrl(next || '/month');
      },
      error: (e) => {
        this.error.set(this.auth.describe(e));
        this.busy.set(false);
        this.code.set('');
      },
    });
  }
}
