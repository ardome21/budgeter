import { Routes } from '@angular/router';

import { authGuard } from './auth/auth.guard';

/**
 * Every data route sits behind `authGuard`. That is a convenience, not the
 * protection — the API refuses unauthenticated requests itself, and a guard
 * that can be skipped by editing the URL never stood between anyone and the
 * data. What it buys is not rendering a page of empty tables and 401s.
 */
export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'month' },
  {
    path: 'login',
    title: 'Sign in · budgeter',
    loadComponent: () => import('./auth/login').then((m) => m.Login),
  },
  {
    path: 'setup',
    title: 'Set up · budgeter',
    loadComponent: () => import('./auth/setup').then((m) => m.Setup),
  },
  {
    path: 'month',
    title: 'Month · budgeter',
    canActivate: [authGuard],
    loadComponent: () => import('./month/month').then((m) => m.Month),
  },
  {
    path: 'transactions',
    title: 'Transactions · budgeter',
    canActivate: [authGuard],
    loadComponent: () =>
      import('./transactions/transactions').then((m) => m.Transactions),
  },
  {
    path: 'linked',
    title: 'Linked banks · budgeter',
    canActivate: [authGuard],
    loadComponent: () => import('./linked/linked').then((m) => m.Linked),
  },
  {
    path: 'import',
    title: 'Import · budgeter',
    canActivate: [authGuard],
    loadComponent: () => import('./import/import').then((m) => m.Import),
  },
  {
    path: 'settings',
    title: 'Settings · budgeter',
    canActivate: [authGuard],
    loadComponent: () => import('./settings/settings').then((m) => m.Settings),
  },
  {
    path: 'accounts',
    title: 'Accounts · budgeter',
    canActivate: [authGuard],
    loadComponent: () => import('./accounts/accounts').then((m) => m.Accounts),
  },
  { path: '**', redirectTo: 'month' },
];
