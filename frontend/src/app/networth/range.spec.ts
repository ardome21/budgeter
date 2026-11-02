import { rangeStart } from './networth';

describe('rangeStart', () => {
  it('counts back from the latest day, inclusive of the day after', () => {
    expect(rangeStart('1m', '2026-11-02')).toBe('2026-10-03');
    expect(rangeStart('3m', '2026-11-02')).toBe('2026-08-03');
    expect(rangeStart('1y', '2026-11-02')).toBe('2025-11-03');
    expect(rangeStart('ytd', '2026-11-02')).toBe('2026-01-01');
  });

  it('means everything for All, or when there is nothing yet', () => {
    expect(rangeStart('all', '2026-11-02')).toBeNull();
    expect(rangeStart('1m', undefined)).toBeNull();
  });
});
