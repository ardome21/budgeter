import { Component, effect, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { AccountMenu } from './auth/account-menu';
import { Auth } from './auth/auth';
import { Sync } from './sync';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, AccountMenu],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App {
  auth = inject(Auth);
  sync = inject(Sync);

  constructor() {
    // One status call at startup so the shell knows whether to draw the nav.
    // The guard re-checks per navigation; this is only about what the chrome
    // shows before the first route resolves.
    this.auth.refresh().subscribe({ error: () => {} });

    // The first visit of the day refreshes every bank, whichever screen it
    // lands on. Waits for sign-in, because the API refuses anything before.
    effect(() => {
      if (this.auth.signedIn()) this.sync.daily();
    });
  }

  updated(at: Date): string {
    const today = at.toDateString() === new Date().toDateString();
    return today
      ? at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
      : at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
}
