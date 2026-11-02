import { Pipe, PipeTransform } from '@angular/core';

/**
 * `"-1234.5"` → `"−$1,234.50"`. Money arrives from the API as a decimal
 * string and is formatted as one: never parsed to a float, which is the thing
 * this codebase refuses to do with money anywhere. Rewriting the string is
 * exact by construction; `Intl.NumberFormat` would need a number first.
 *
 * A leading `+` is kept (the ledger marks money in with it). More than two
 * decimal places are kept where they mean something — a fund priced at
 * 22.7034 — and trimmed back to two where they are only padding. Null stays
 * null, so a template can still say `(x | usd) ?? '—'`.
 */
@Pipe({ name: 'usd' })
export class UsdPipe implements PipeTransform {
  transform(value: string | null | undefined): string | null {
    return usd(value);
  }
}

export function usd(value: string | null | undefined): string | null {
  if (value === null || value === undefined || value.trim() === '') return null;

  let digits = value.trim();
  let sign = '';
  if (digits[0] === '-' || digits[0] === '+') {
    sign = digits[0] === '-' ? '−' : '+';
    digits = digits.slice(1);
  }

  const [whole, fraction = ''] = digits.split('.');
  const cents = fraction.replace(/0+$/, '').padEnd(2, '0');
  const grouped = (whole.replace(/^0+(?=\d)/, '') || '0').replace(
    /\B(?=(\d{3})+(?!\d))/g,
    ',',
  );

  // −$0.00 says something that is not true.
  if (/^[0,]*$/.test(grouped) && /^0*$/.test(cents)) sign = '';
  return `${sign}$${grouped}.${cents}`;
}
