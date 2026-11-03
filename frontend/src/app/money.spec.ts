import { change, fromCents, toCents } from './money';

describe('money', () => {
  it('reads what people type', () => {
    expect(toCents('1,610')).toBe(161000n);
    expect(toCents('$842.17')).toBe(84217n);
    expect(toCents('.5')).toBe(50n);
    expect(toCents('12.')).toBe(1200n);
  });

  it('refuses what is not an amount', () => {
    expect(toCents('')).toBeNull();
    expect(toCents('abc')).toBeNull();
    expect(toCents('1.234')).toBeNull();
    expect(toCents('.')).toBeNull();
  });

  it('adds without drifting, where floats do', () => {
    const sum = [toCents('0.10')!, toCents('0.20')!].reduce((a, b) => a + b);
    expect(fromCents(sum)).toBe('0.30');
    expect(fromCents(-5n)).toBe('-0.05');
  });

  it('takes one day from another, signed, to as many places as either has', () => {
    expect(change('1200.50', '1000.00')).toBe('+200.50');
    expect(change('90.00', '100.25')).toBe('-10.25');
    expect(change('-300.00', '-500.00')).toBe('+200.00');
    expect(change('0.10', '0.10')).toBe('0.00');
    expect(change('22.7034', '22.70')).toBe('+0.0034');
    expect(change('0.30', '0.95')).toBe('-0.65');
  });
});
