import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { map } from 'rxjs';

import { Auth } from './auth';

/**
 * Nothing renders until /auth/status has answered.
 *
 * The status is re-fetched rather than trusted from the signal, because the
 * session can expire while the tab sits open — the alternative is a screen
 * full of empty tables and a console full of 401s.
 *
 * This is a convenience, not the protection. The API refuses unauthenticated
 * requests on its own; a guard that can be skipped by editing the URL was
 * never what stood between anyone and the data.
 */
export const authGuard: CanActivateFn = (_route, state) => {
  const auth = inject(Auth);
  const router = inject(Router);

  return auth.refresh().pipe(
    map((status) => {
      if (!status.configured) return router.parseUrl('/setup');
      if (!status.authenticated) {
        // Remember where they were headed, so signing in lands there rather
        // than dumping everyone on the month view.
        return router.createUrlTree(['/login'], {
          queryParams: state.url === '/' ? null : { next: state.url },
        });
      }
      return true;
    }),
  );
};
