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
    path: 'merchants',
    title: 'Merchants · budgeter',
    loadComponent: () => import('./merchants/workbench').then((m) => m.Workbench),
  },
  {
    // The suggestion queue, reachable from the workbench when it has items.
    path: 'merchants/review',
    title: 'Review merchants · budgeter',
    loadComponent: () =>
      import('./merchants/merchants').then((m) => m.Merchants),
  },
  { path: '**', redirectTo: 'month' },
];
