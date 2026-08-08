import { HttpErrorResponse } from '@angular/common/http';
import { provideHttpClient } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { Auth } from './auth';

/**
 * These exist for one regression: a rejected field came back as
 * "That did not work."
 *
 * FastAPI answers a validation failure with `detail` as an array of
 * {loc, msg}, not a string. Handling only the string case meant a password two
 * characters short produced a generic message, and setting up the app looked
 * broken rather than merely refused.
 */
describe('Auth.describe', () => {
  let auth: Auth;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideRouter([])],
    });
    auth = TestBed.inject(Auth);
  });

  function error(status: number, body: unknown): HttpErrorResponse {
    return new HttpErrorResponse({ status, error: body });
  }

  it('names the field a validation error came from', () => {
    const message = auth.describe(
      error(422, {
        detail: [
          {
            type: 'string_too_short',
            loc: ['body', 'password'],
            msg: 'String should have at least 12 characters',
          },
        ],
      }),
    );

    expect(message).toContain('Password');
    expect(message).toContain('12 characters');
    expect(message).not.toBe('That did not work.');
  });

  it('reports every rejected field, not just the first', () => {
    const message = auth.describe(
      error(422, {
        detail: [
          { loc: ['body', 'username'], msg: 'String should have at least 1 character' },
          { loc: ['body', 'password'], msg: 'String should have at least 12 characters' },
        ],
      }),
    );

    expect(message).toContain('Username');
    expect(message).toContain('Password');
  });

  it('passes a plain string detail through unchanged', () => {
    expect(auth.describe(error(401, { detail: 'that did not match' }))).toBe(
      'that did not match',
    );
  });

  it('says the backend is unreachable rather than blaming the input', () => {
    expect(auth.describe(error(0, null))).toContain('Cannot reach the API');
  });

  it('falls back to something rather than crashing on an odd shape', () => {
    expect(auth.describe(error(500, { detail: { unexpected: true } }))).toBe(
      'That did not work.',
    );
  });
});
