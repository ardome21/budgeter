import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import {
  Category,
  CommitResult,
  Merchant,
  MonthSummary,
  NewTransaction,
  Overview,
  Period,
  Preview,
  Transaction,
} from './models';

/**
 * The one place that knows about HTTP.
 *
 * Requests go to the relative path /api, which the dev server proxies to the
 * backend, so the browser only ever sees a single origin.
 */
@Injectable({ providedIn: 'root' })
export class Api {
  private http = inject(HttpClient);

  categories(): Observable<Category[]> {
    return this.http.get<Category[]>('/api/categories');
  }

  periods(): Observable<Period[]> {
    return this.http.get<Period[]>('/api/periods');
  }

  summary(year: number, month: number): Observable<MonthSummary> {
    return this.http.get<MonthSummary>(`/api/periods/${year}/${month}/summary`);
  }

  overview(): Observable<Overview> {
    return this.http.get<Overview>('/api/overview');
  }

  transactions(params: {
    year?: number;
    month?: number;
    category_id?: number;
    q?: string;
    limit?: number;
  }): Observable<Transaction[]> {
    const query = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
      .join('&');
    return this.http.get<Transaction[]>(
      `/api/transactions${query ? '?' + query : ''}`,
    );
  }

  createTransaction(body: NewTransaction): Observable<Transaction> {
    return this.http.post<Transaction>('/api/transactions', body);
  }

  updateTransaction(
    id: number,
    body: Partial<NewTransaction>,
  ): Observable<Transaction> {
    return this.http.patch<Transaction>(`/api/transactions/${id}`, body);
  }

  deleteTransaction(id: number): Observable<void> {
    return this.http.delete<void>(`/api/transactions/${id}`);
  }

  previewCsv(text: string, flipSign: boolean): Observable<Preview> {
    const form = new FormData();
    form.append('text', text);
    form.append('flip_sign', String(flipSign));
    return this.http.post<Preview>('/api/imports/preview', form);
  }

  previewCsvFile(file: File, flipSign: boolean): Observable<Preview> {
    const form = new FormData();
    form.append('file', file);
    form.append('flip_sign', String(flipSign));
    return this.http.post<Preview>('/api/imports/preview', form);
  }

  commitImport(rows: unknown[]): Observable<CommitResult> {
    return this.http.post<CommitResult>('/api/imports/commit', { rows });
  }

  merchants(q?: string): Observable<Merchant[]> {
    return this.http.get<Merchant[]>(
      `/api/merchants${q ? '?q=' + encodeURIComponent(q) : ''}`,
    );
  }

  mergeMerchant(id: number, intoId: number): Observable<Merchant> {
    return this.http.post<Merchant>(`/api/merchants/${id}/merge`, {
      into_id: intoId,
    });
  }
}
