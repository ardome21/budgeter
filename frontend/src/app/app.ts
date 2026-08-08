import { Component, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { Auth } from './auth/auth';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  templateUrl: './app.html',
  styleUrl: './app.scss',
})
export class App {
  auth = inject(Auth);

  constructor() {
    // One status call at startup so the shell knows whether to draw the nav.
    // The guard re-checks per navigation; this is only about what the chrome
    // shows before the first route resolves.
    this.auth.refresh().subscribe({ error: () => {} });
  }
}
