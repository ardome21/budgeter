import { fromCents, toCents } from './money';

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
});
