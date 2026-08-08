import { base64urlToBuffer, bufferToBase64url } from './webauthn';

/**
 * base64url, both directions.
 *
 * This is the one part of a passkey integration that can be wrong quietly.
 * A bad conversion does not throw — it produces a slightly different byte
 * string, the signature fails to verify, and the only symptom is "that did not
 * verify" with nothing to suggest the encoding is at fault.
 *
 * The padding cases are where it actually goes wrong: base64url strips `=`,
 * and `atob` requires it.
 */
describe('base64url', () => {
  function bytes(buffer: ArrayBuffer): number[] {
    return Array.from(new Uint8Array(buffer));
  }

  it('round-trips at every padding length', () => {
    // 1, 2 and 3 bytes hit the three padding cases: '==', '=' and none.
    for (const input of [[1], [1, 2], [1, 2, 3], [1, 2, 3, 4]]) {
      const encoded = bufferToBase64url(new Uint8Array(input).buffer);
      expect(bytes(base64urlToBuffer(encoded))).toEqual(input);
    }
  });

  it('round-trips the full byte range', () => {
    const all = Array.from({ length: 256 }, (_, i) => i);
    const encoded = bufferToBase64url(new Uint8Array(all).buffer);
    expect(bytes(base64urlToBuffer(encoded))).toEqual(all);
  });

  it('emits url-safe output with no padding', () => {
    // 0xfb 0xff would be '+/' in standard base64 — the two characters that
    // must not appear, since these values travel in URLs and JSON.
    const encoded = bufferToBase64url(new Uint8Array([251, 255, 190]).buffer);
    expect(encoded).not.toContain('+');
    expect(encoded).not.toContain('/');
    expect(encoded).not.toContain('=');
  });

  it('decodes url-safe input that standard base64 would reject', () => {
    expect(bytes(base64urlToBuffer('-_8'))).toEqual([251, 255]);
  });

  it('handles an empty buffer', () => {
    expect(bufferToBase64url(new Uint8Array([]).buffer)).toBe('');
    expect(bytes(base64urlToBuffer(''))).toEqual([]);
  });

  it('round-trips something the length of a real challenge', () => {
    const challenge = Array.from({ length: 32 }, (_, i) => (i * 7 + 3) % 256);
    const encoded = bufferToBase64url(new Uint8Array(challenge).buffer);
    expect(bytes(base64urlToBuffer(encoded))).toEqual(challenge);
  });
});
