import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import {
  AccountRow,
  Budget,
  Category,
  LinkedItem,
  MonthSummary,
  NetWorth,
  Portfolio,
  Preview,
  Transaction,
  TransactionPage,
} from './models';

/**
 * The one place that knows about HTTP.
 *
 * Requests go to the relative path /api, which the dev server proxies to the
 * backend, so the browser only ever sees a single origin and no CORS
 * preflight stands between a click and the data.
 */
@Injectable({ providedIn: 'root' })
export class Api {
  private http = inject(HttpClient);

  // --- the month ------------------------------------------------------------

  months(): Observable<string[]> {
    return this.http.get<string[]>('/api/months');
  }

  month(year: number, month: number): Observable<MonthSummary> {
    return this.http.get<MonthSummary>(`/api/month/${year}/${month}`);
  }

  // --- net worth ------------------------------------------------------------

  netWorth(days = 365): Observable<NetWorth> {
    return this.http.get<NetWorth>('/api/net-worth', {
      params: new HttpParams().set('days', days),
    });
  }

  portfolio(): Observable<Portfolio> {
    return this.http.get<Portfolio>('/api/portfolio');
  }

  accounts(includeClosed = false): Observable<AccountRow[]> {
    return this.http.get<AccountRow[]>('/api/accounts', {
      params: new HttpParams().set('include_closed', includeClosed),
    });
  }

  updateAccount(
    id: number,
    patch: { is_retirement?: boolean; closed_on?: string | null },
  ): Observable<AccountRow> {
    return this.http.patch<AccountRow>(`/api/accounts/${id}`, patch);
  }

  // --- the ledger -----------------------------------------------------------

  transactions(filter: {
    month?: string;
    category_id?: number;
    account_id?: number;
    search?: string;
    limit?: number;
    offset?: number;
  }): Observable<TransactionPage> {
    let params = new HttpParams();
    for (const [key, value] of Object.entries(filter)) {
      if (value !== undefined && value !== null && value !== '') {
        params = params.set(key, value);
      }
    }
    return this.http.get<TransactionPage>('/api/transactions', { params });
  }

  updateTransaction(
    id: number,
    patch: { category_id?: number; merchant?: string; notes?: string },
  ): Observable<Transaction> {
    return this.http.patch<Transaction>(`/api/transactions/${id}`, patch);
  }

  merchants(): Observable<string[]> {
    return this.http.get<string[]>('/api/merchants');
  }

  // --- categories and budgets ----------------------------------------------

  categories(): Observable<Category[]> {
    return this.http.get<Category[]>('/api/categories');
  }

  createCategory(body: Partial<Category>): Observable<Category> {
    return this.http.post<Category>('/api/categories', body);
  }

  updateCategory(id: number, body: Partial<Category>): Observable<Category> {
    return this.http.patch<Category>(`/api/categories/${id}`, body);
  }

  deleteCategory(id: number): Observable<void> {
    return this.http.delete<void>(`/api/categories/${id}`);
  }

  budgets(year: number, month: number): Observable<Budget[]> {
    return this.http.get<Budget[]>(`/api/budgets/${year}/${month}`);
  }

  setBudgets(
    year: number,
    month: number,
    rows: Budget[],
  ): Observable<Budget[]> {
    return this.http.put<Budget[]>(`/api/budgets/${year}/${month}`, rows);
  }

  copyBudgets(year: number, month: number): Observable<Budget[]> {
    return this.http.post<Budget[]>(`/api/budgets/${year}/${month}/copy`, {});
  }

  // --- linked banks ---------------------------------------------------------

  linkedItems(): Observable<LinkedItem[]> {
    return this.http.get<LinkedItem[]>('/api/linked/items');
  }

  linkToken(products: string[], itemId?: number): Observable<{ link_token: string }> {
    return this.http.post<{ link_token: string }>('/api/linked/link-token', {
      products,
      item_id: itemId ?? null,
    });
  }

  exchange(publicToken: string, products: string[]): Observable<LinkedItem> {
    return this.http.post<LinkedItem>('/api/linked/exchange', {
      public_token: publicToken,
      products,
    });
  }

  preview(itemId: number): Observable<Preview> {
    return this.http.post<Preview>(`/api/linked/items/${itemId}/preview`, {});
  }

  commit(itemId: number, accept: string[]): Observable<Preview> {
    return this.http.post<Preview>('/api/linked/commit', {
      item_id: itemId,
      accept,
    });
  }

  rewind(itemId: number): Observable<void> {
    return this.http.post<void>(`/api/linked/items/${itemId}/rewind`, {});
  }

  unlink(itemId: number): Observable<void> {
    return this.http.delete<void>(`/api/linked/items/${itemId}`);
  }
}
