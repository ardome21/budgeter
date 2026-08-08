import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';

import { Auth } from './auth';
import { SetupResult } from '../models';

/**
 * First run: claim the app.
 *
 * Two steps on purpose. Creating the account hands back a secret and ten
 * recovery codes; the account is not usable until a code generated from that
 * secret comes back. A mistyped scan would otherwise lock the only person
 * there will ever be out of their own three years of history.
 */
@Component({
  selector: 'app-setup',
  imports: [FormsModule],
  template: `
    <div class="gate">
      <div class="card wide">
        @if (!result()) {
          <h1>Set up budgeter</h1>
          <p class="dim small">
            Nobody has claimed this app yet. Pick a name and a password — you
            will add a second factor on the next step.
          </p>

          @if (error(); as e) {
            <div class="banner error">{{ e }}</div>
          }

          <form (ngSubmit)="create()">
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
              autocomplete="new-password"
              [ngModel]="password()"
              (ngModelChange)="password.set($event)"
              required
            />
            <p class="small hint" [class.dim]="longEnough()" [class.over]="!longEnough() && password().length > 0">
              At least 12 characters — {{ password().length }} so far.
            </p>

            <button
              class="primary"
              type="submit"
              [disabled]="busy() || !canSubmit()"
            >
              {{ busy() ? 'Creating…' : 'Continue' }}
            </button>
          </form>
        } @else {
          <h1>Two things, one step</h1>

          <section class="step">
            <h2><span class="n">1</span> Scan this</h2>
            <div class="qr" [innerHTML]="qr()"></div>
            <p class="small">
              Any authenticator works, including Apple Passwords — scanning
              with the Camera and letting it save there is exactly right.
            </p>
            <p class="small dim">
              Afterwards it shows a <strong>6-digit code that changes every 30
              seconds</strong>. On a Mac or iPhone that is the
              <em>Verification Code</em> row under <em>budgeter</em> in the
              Passwords app. You will type it in below.
            </p>
            <details>
              <summary class="small dim">Can't scan it?</summary>
              <p class="small dim">Type this into your authenticator instead:</p>
              <code class="secret">{{ secret() }}</code>
            </details>
          </section>

          <section class="step">
            <h2><span class="n">2</span> Keep these somewhere safe</h2>
            <p class="small">
              <strong>Nothing to do with them now.</strong> They are a spare
              key, not a step — copy them somewhere you will still have if your
              phone is gone, then carry on.
            </p>
            <div class="banner warn codes">
              <ul>
                @for (c of result()!.recovery_codes; track c) {
                  <li><code>{{ c }}</code></li>
                }
              </ul>
              <button type="button" (click)="copyCodes()">
                {{ copied() ? 'Copied' : 'Copy all' }}
              </button>
              <p class="small dim tail">
                Each works once, in place of the 6-digit code, if you ever lose
                the authenticator. They are never shown again.
              </p>
            </div>
          </section>

          @if (error(); as e) {
            <div class="banner error">{{ e }}</div>
          }

          <form (ngSubmit)="confirm()">
            <label for="c">
              Now type the 6-digit code from your authenticator
            </label>
            <input
              id="c"
              name="code"
              inputmode="numeric"
              autocomplete="one-time-code"
              placeholder="123456"
              [ngModel]="code()"
              (ngModelChange)="code.set($event)"
              required
            />
            <p class="small dim hint">
              If the countdown is about to run out, wait for the next one — any
              current code works.
            </p>
            <button class="primary" type="submit" [disabled]="busy()">
              {{ busy() ? 'Checking…' : 'Finish' }}
            </button>
          </form>
        }
      </div>
    </div>
  `,
  styleUrl: './gate.scss',
})
export class Setup {
  private auth = inject(Auth);
  private router = inject(Router);
  private sanitizer = inject(DomSanitizer);

  username = signal('');
  password = signal('');
  code = signal('');
  busy = signal(false);
  copied = signal(false);
  error = signal<string | null>(null);
  result = signal<SetupResult | null>(null);
  qr = signal<SafeHtml | null>(null);

  /** The bare secret, for anyone whose authenticator cannot scan. */
  secret = signal<string | null>(null);

  // Mirrors the backend's own rule. Checked here so the length requirement is
  // visible while typing rather than delivered as a rejection afterwards.
  longEnough = computed(() => this.password().length >= 12);
  canSubmit = computed(
    () => this.username().trim().length > 0 && this.longEnough(),
  );

  create(): void {
    this.busy.set(true);
    this.error.set(null);
    this.auth.setup(this.username(), this.password()).subscribe({
      next: (r) => {
        this.result.set(r);
        // The SVG is generated by our own backend from a URI we sent it, so
        // there is no third-party markup here to be wary of.
        this.qr.set(this.sanitizer.bypassSecurityTrustHtml(r.qr_svg));
        this.secret.set(new URL(r.otpauth_uri).searchParams.get('secret'));
        this.busy.set(false);
      },
      error: (e) => {
        this.error.set(this.auth.describe(e));
        this.busy.set(false);
      },
    });
  }

  confirm(): void {
    this.busy.set(true);
    this.error.set(null);
    this.auth.confirmSetup(this.code()).subscribe({
      next: () => {
        this.busy.set(false);
        this.router.navigateByUrl('/month');
      },
      error: (e) => {
        this.error.set(this.auth.describe(e));
        this.busy.set(false);
        this.code.set('');
      },
    });
  }

  copyCodes(): void {
    const codes = this.result()?.recovery_codes.join('\n') ?? '';
    navigator.clipboard.writeText(codes).then(
      () => this.copied.set(true),
      () => this.error.set('Could not copy — select and copy them by hand.'),
    );
  }
}
