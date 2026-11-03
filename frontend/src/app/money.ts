/**
 * Adding up money the user is typing, exactly.
 *
 * Everything else on screen was summed by the server, but a total that moves
 * as you type cannot wait on a request. So the amounts are read as whole
 * cents into a `bigint` — integers, never a float — and written back as the
 * same decimal string the API speaks, for the `usd` pipe to format.
 *
 * The one other sum done here is a change between two days the server has
 * already totalled: picked by dragging across a chart, so again it cannot
 * wait on a request, and again it is done in integers.
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

/**
 * `later − earlier`, exactly, signed the way the ledger signs money in:
 * ("1200.50", "1000") → "+200.50", ("90", "100.25") → "-10.25". Takes as
 * many decimal places as either side has, so a fund's 22.7034 survives.
 */
export function change(later: string, earlier: string): string {
  const places = Math.max(2, decimals(later), decimals(earlier));
  const d = scaled(later, places) - scaled(earlier, places);
  const sign = d > 0n ? '+' : d < 0n ? '-' : '';
  const abs = (d < 0n ? -d : d).toString().padStart(places + 1, '0');
  return `${sign}${abs.slice(0, -places)}.${abs.slice(-places)}`;
}

function decimals(value: string): number {
  return value.split('.')[1]?.length ?? 0;
}

/** "-12.5" at 3 places → -12500n. */
function scaled(value: string, places: number): bigint {
  const negative = value.trim().startsWith('-');
  const [whole, fraction = ''] = value.trim().replace(/^[-+]/, '').split('.');
  const n = BigInt(whole || '0') * 10n ** BigInt(places) + BigInt(fraction.padEnd(places, '0') || '0');
  return negative ? -n : n;
}
