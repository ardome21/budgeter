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
  AccountRow,
  Allocations,
  FixedCost,
  MerchantPage,
  MerchantSort,
  NetWorth,
  PaycheckLine,
  Reconciliation,
  Suggestion,
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
      merchant_id: number | null;
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

  merchants(
    q: string,
    sort: MerchantSort,
    limit = 100,
    offset = 0,
  ): Observable<MerchantPage> {
    const params = new URLSearchParams({
      sort,
      limit: String(limit),
      offset: String(offset),
    });
    if (q) params.set('q', q);
    return this.http.get<MerchantPage>(`/api/merchants?${params}`);
  }

  /**
   * Fold several merchants into one, optionally renaming the survivor.
   * The suggestion rule keys on the first word, so it can never propose
   * 'Airbnb' and 'Revolution Park Air Bnb' — those get picked by hand.
   */
  mergeMany(
    sourceIds: number[],
    intoId: number,
    canonicalName?: string,
  ): Observable<Merchant> {
    return this.http.post<Merchant>('/api/merchants/merge', {
      source_ids: sourceIds,
      into_id: intoId,
      canonical_name: canonicalName ?? null,
    });
  }

  suggestions(): Observable<Suggestion[]> {
    return this.http.get<Suggestion[]>('/api/merchants/suggestions');
  }

  /**
   * Record that names are different places.
   * With `anchor`, only anchor-to-each pairs are recorded — the partial case
   * after merging some of a group.
   */
  rejectSuggestion(names: string[], anchor?: string): Observable<void> {
    return this.http.post<void>('/api/merchants/suggestions/reject', {
      names,
      anchor: anchor ?? null,
    });
  }

  /** Give a merchant a name of your own. Split records follow the rename. */
  renameMerchant(id: number, canonicalName: string): Observable<Merchant> {
    return this.http.patch<Merchant>(`/api/merchants/${id}`, {
      canonical_name: canonicalName,
    });
  }

  mergeMerchant(id: number, intoId: number): Observable<Merchant> {
    return this.http.post<Merchant>(`/api/merchants/${id}/merge`, {
      into_id: intoId,
    });
  }
}
