import { usd } from './usd';

describe('usd', () => {
  it('groups thousands and pads cents, from the string as sent', () => {
    expect(usd('182079.6')).toBe('$182,079.60');
    expect(usd('1234567.89')).toBe('$1,234,567.89');
    expect(usd('5.00')).toBe('$5.00');
  });

  it('puts the sign before the dollar sign', () => {
    expect(usd('-537.24')).toBe('−$537.24');
    expect(usd('+12.00')).toBe('+$12.00');
  });

  it('keeps precision that means something, and drops padding', () => {
    expect(usd('22.7034')).toBe('$22.7034');
    expect(usd('22.7000')).toBe('$22.70');
  });

  it('never reads as negative zero, and leaves null alone', () => {
    expect(usd('-0.00')).toBe('$0.00');
    expect(usd(null)).toBeNull();
    expect(usd('')).toBeNull();
  });
});
