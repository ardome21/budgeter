/**
 * Adding up money the user is typing, exactly.
 *
 * Everything else on screen was summed by the server, but a total that moves
 * as you type cannot wait on a request. So the amounts are read as whole
 * cents into a `bigint` — integers, never a float — and written back as the
 * same decimal string the API speaks, for the `usd` pipe to format.
 */

/** "1,610" or "$1610.5" → 161050n. Null for anything that is not an amount. */
export function toCents(value: string): bigint | null {
  const clean = value.trim().replace(/[$,\s]/g, '');
  const match = /^(-?)(\d*)(?:\.(\d{0,2}))?$/.exec(clean);
  if (!match || (!match[2] && !match[3])) return null;
  const [, sign, whole, fraction = ''] = match;
  const cents = BigInt(whole || '0') * 100n + BigInt(fraction.padEnd(2, '0'));
  return sign ? -cents : cents;
}

/** 161050n → "1610.50". */
export function fromCents(cents: bigint): string {
  const sign = cents < 0n ? '-' : '';
  const abs = cents < 0n ? -cents : cents;
  return `${sign}${abs / 100n}.${String(abs % 100n).padStart(2, '0')}`;
}
