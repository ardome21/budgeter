import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import {
  Category,
  CommitResult,
  MerchantKey,
  MonthSummary,
  NewTransaction,
  Overview,
  Period,
  Preview,
  AccountRow,
  Allocations,
  FixedCost,
  NetWorth,
  PaycheckLine,
  Reconciliation,
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

  previewCsv(
    text: string,
    flipSign: boolean,
    accountId: number | null,
  ): Observable<Preview> {
    const form = new FormData();
    form.append('text', text);
    return this.http.post<Preview>(
      '/api/imports/preview',
      this.previewForm(form, flipSign, accountId),
    );
  }

  previewCsvFile(
    file: File,
    flipSign: boolean,
    accountId: number | null,
  ): Observable<Preview> {
    const form = new FormData();
    form.append('file', file);
    return this.http.post<Preview>(
      '/api/imports/preview',
      this.previewForm(form, flipSign, accountId),
    );
  }

  /** The account has to reach the preview, not just the commit: it goes into
   *  each row's hash, so leaving it off would hash every row twice over. */
  private previewForm(
    form: FormData,
    flipSign: boolean,
    accountId: number | null,
  ): FormData {
    form.append('flip_sign', String(flipSign));
    if (accountId !== null) form.append('account_id', String(accountId));
    return form;
  }

  commitImport(
    rows: unknown[],
    accountId: number | null,
  ): Observable<CommitResult> {
    return this.http.post<CommitResult>('/api/imports/commit', {
      rows,
      account_id: accountId,
    });
  }

  allocations(year: number, month: number): Observable<Allocations> {
    return this.http.get<Allocations>(
      `/api/periods/${year}/${month}/allocations`,
    );
  }

  setAllocations(
    year: number,
    month: number,
    allocations: { category_id: number; amount: string }[],
  ): Observable<Allocations> {
    return this.http.put<Allocations>(
      `/api/periods/${year}/${month}/allocations`,
      { allocations },
    );
  }

  copyAllocations(
    year: number,
    month: number,
    fromYear: number,
    fromMonth: number,
  ): Observable<Allocations> {
    return this.http.post<Allocations>(
      `/api/periods/${year}/${month}/allocations/copy?from_year=${fromYear}&from_month=${fromMonth}`,
      {},
    );
  }

  fixedCosts(includeEnded = false): Observable<FixedCost[]> {
    return this.http.get<FixedCost[]>(
      `/api/fixed-costs?include_ended=${includeEnded}`,
    );
  }

  updateFixedCost(
    id: number,
    body: Partial<{
      description: string;
      amount: string;
      category_id: number;
      merchant_key: string | null;
    }>,
  ): Observable<FixedCost> {
    return this.http.patch<FixedCost>(`/api/fixed-costs/${id}`, body);
  }

  endFixedCost(id: number): Observable<void> {
    return this.http.delete<void>(`/api/fixed-costs/${id}`);
  }

  paycheck(): Observable<PaycheckLine[]> {
    return this.http.get<PaycheckLine[]>('/api/paycheck');
  }

  updatePaycheckLine(
    id: number,
    body: Partial<{ description: string; amount: string }>,
  ): Observable<PaycheckLine> {
    return this.http.patch<PaycheckLine>(`/api/paycheck/${id}`, body);
  }

  reconcile(year: number, month: number): Observable<Reconciliation> {
    return this.http.get<Reconciliation>(
      `/api/periods/${year}/${month}/reconcile`,
    );
  }

  accounts(): Observable<AccountRow[]> {
    return this.http.get<AccountRow[]>('/api/accounts');
  }

  /** Settle an account: history stays, but it stops reading as current. */
  closeAccount(id: number, closedOn: string | null): Observable<AccountRow> {
    return this.http.patch<AccountRow>(`/api/accounts/${id}`, {
      closed_on: closedOn,
    });
  }

  netWorth(): Observable<NetWorth> {
    return this.http.get<NetWorth>('/api/accounts/net-worth');
  }

  /** PUT: a snapshot is identified by its date, so a second reading on the
   *  same day is a correction rather than an additional holding. */
  recordBalance(
    accountId: number,
    asOf: string,
    balance: string,
  ): Observable<unknown> {
    return this.http.put(`/api/accounts/${accountId}/balances`, {
      as_of: asOf,
      balance,
    });
  }

  /**
   * Merchant names already in use, most-used first.
   *
   * All that is left of the merchant screens. Offering what exists at the
   * moment of entry is what stops a second spelling getting in — which is the
   * job the merge queue used to do afterwards, badly.
   */
  merchantKeys(q = '', limit = 20): Observable<MerchantKey[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (q) params.set('q', q);
    return this.http.get<MerchantKey[]>(`/api/merchants/keys?${params}`);
  }
}
