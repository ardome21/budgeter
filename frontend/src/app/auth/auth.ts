import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Observable, tap } from 'rxjs';

import { AuthStatus, SetupResult } from '../models';

/**
 * Who is signed in, and the calls that change that.
 *
 * The signal is the single source of truth for the shell and the guard, so a
 * 401 anywhere — including one raised by the interceptor after a session has
 * quietly expired — puts the whole app back to the login screen at once,
 * rather than leaving half of it rendering stale data.
 */
@Injectable({ providedIn: 'root' })
export class Auth {
  private http = inject(HttpClient);
  private router = inject(Router);

  status = signal<AuthStatus | null>(null);

  /** Null until /auth/status has answered — the shell waits rather than
   *  flashing a login screen at someone who is already signed in. */
  known = computed(() => this.status() !== null);
  signedIn = computed(() => this.status()?.authenticated === true);
  /** False before anyone has claimed the app: setup, not login. */
  configured = computed(() => this.status()?.configured === true);
  username = computed(() => this.status()?.username ?? null);

  refresh(): Observable<AuthStatus> {
    return this.http
      .get<AuthStatus>('/api/auth/status')
      .pipe(tap((s) => this.status.set(s)));
  }

  setup(username: string, password: string): Observable<SetupResult> {
    return this.http.post<SetupResult>('/api/auth/setup', {
      username,
      password,
    });
  }

  confirmSetup(code: string): Observable<{ username: string }> {
    return this.http
      .post<{ username: string }>('/api/auth/setup/confirm', { code })
      .pipe(tap(() => this.refresh().subscribe()));
  }

  login(
    username: string,
    password: string,
    code: string,
  ): Observable<{ username: string }> {
    return this.http
      .post<{ username: string }>('/api/auth/login', {
        username,
        password,
        code,
      })
      .pipe(tap(() => this.refresh().subscribe()));
  }

  logout(): void {
    this.http.post('/api/auth/logout', {}).subscribe({
      next: () => this.afterSignOut(),
      // Already signed out server-side is still signed out here.
      error: () => this.afterSignOut(),
    });
  }

  /** Called by the interceptor when any request comes back 401. */
  afterSignOut(): void {
    this.status.update((s) =>
      s ? { ...s, authenticated: false, username: null } : s,
    );
    this.router.navigateByUrl('/login');
  }

  /**
   * Turn an error response into something worth reading.
   *
   * FastAPI answers a rejected field with `detail` as an *array* of
   * `{loc, msg}`, not a string. Handling only the string case meant a password
   * two characters short came back as "That did not work." — the app knew
   * exactly what was wrong and declined to say so.
   */
  describe(e: unknown): string {
    const err = e as HttpErrorResponse;
    if (err?.status === 0)
      return 'Cannot reach the API. Is the backend running?';

    const detail = (err?.error as { detail?: unknown })?.detail;
    if (typeof detail === 'string') return detail;

    if (Array.isArray(detail)) {
      const problems = (detail as { loc?: unknown[]; msg?: string }[])
        .map((d) => {
          // loc is ['body', 'password']; the field is the useful part.
          const field = String(d.loc?.[d.loc.length - 1] ?? '');
          const message = d.msg ?? 'is not valid';
          if (!field || field === 'body') return message;
          const label = field.charAt(0).toUpperCase() + field.slice(1);
          return `${label}: ${message.replace(/^String /, '')}`;
        })
        .filter(Boolean);
      if (problems.length) return problems.join(' · ');
    }

    return 'That did not work.';
  }
}
