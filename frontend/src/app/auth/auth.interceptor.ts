import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';

import { Auth } from './auth';

/**
 * A 401 from anywhere means the session is gone — say so once, centrally.
 *
 * A session expires while the tab is open, not while anyone is watching. Left
 * to each screen, that surfaces as a different error message per component and
 * a page of stale numbers; here it is one redirect to the login screen.
 *
 * The auth endpoints are exempt: a wrong password is a 401 that the login form
 * has to show, not a reason to bounce back to itself.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(Auth);

  return next(req).pipe(
    catchError((error: unknown) => {
      const is401 = error instanceof HttpErrorResponse && error.status === 401;
      if (is401 && !req.url.startsWith('/api/auth/')) {
        auth.afterSignOut();
      }
      return throwError(() => error);
    }),
  );
};
