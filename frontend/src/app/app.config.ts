import {
  ApplicationConfig,
  provideBrowserGlobalErrorListeners,
  provideZoneChangeDetection,
} from '@angular/core';
import {
  provideHttpClient,
  withFetch,
  withInterceptors,
} from '@angular/common/http';
import { provideRouter } from '@angular/router';

import { routes } from './app.routes';
import { authInterceptor } from './auth/auth.interceptor';

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes),
    // Requests go to the relative path /api, which proxy.conf.json forwards to
    // the backend in dev. Same-origin in the browser, so CORS is not what makes
    // this work — it stays configured only as a safety net.
    // The interceptor turns a 401 from anywhere into one redirect to the login
    // screen, instead of a different stale-data failure per component.
    provideHttpClient(withFetch(), withInterceptors([authInterceptor])),
  ],
};
