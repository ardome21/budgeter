import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';

import { Auth } from './auth';

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
