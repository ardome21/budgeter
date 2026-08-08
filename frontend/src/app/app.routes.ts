import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'month' },
  {
    path: 'month',
    title: 'Month · budgeter',
    loadComponent: () => import('./month/month').then((m) => m.Month),
  },
  {
    path: 'transactions',
    title: 'Transactions · budgeter',
    loadComponent: () =>
      import('./transactions/transactions').then((m) => m.Transactions),
  },
  {
    path: 'import',
    title: 'Import · budgeter',
    loadComponent: () => import('./import/import').then((m) => m.Import),
  },
  {
    path: 'settings',
    title: 'Settings · budgeter',
    loadComponent: () => import('./settings/settings').then((m) => m.Settings),
  },
  {
    path: 'accounts',
    title: 'Accounts · budgeter',
    loadComponent: () => import('./accounts/accounts').then((m) => m.Accounts),
  },
  { path: '**', redirectTo: 'month' },
];
