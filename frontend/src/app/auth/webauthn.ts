/**
 * The conversion the WebAuthn API forces on every caller.
 *
 * `navigator.credentials` speaks ArrayBuffer; JSON does not. So the options
 * arriving from the backend carry base64url strings that have to become
 * buffers, and the credential coming back carries buffers that have to become
 * base64url strings. Nothing here is clever — it is the whole reason a passkey
 * integration looks bigger than it is.
 *
 * Browsers are gaining `parseCreationOptionsFromJSON` to do exactly this, but
 * it is recent enough that hand-rolling costs less than the version check.
 */

export function base64urlToBuffer(value: string): ArrayBuffer {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(padded.padEnd(padded.length + ((4 - (padded.length % 4)) % 4), '='));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export function bufferToBase64url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** True when this browser can do platform passkeys at all. */
export function passkeysSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.PublicKeyCredential !== 'undefined' &&
    typeof navigator.credentials?.create === 'function'
  );
}

/** True when *this device* has a built-in authenticator — Touch ID, Windows
 *  Hello. Offering "sign in with Touch ID" on a machine without one is a
 *  button that can only disappoint. */
export async function platformAuthenticatorAvailable(): Promise<boolean> {
  if (!passkeysSupported()) return false;
  try {
    return await window.PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
  } catch {
    return false;
  }
}

/**
 * The JSON the backend sends, described on its own terms.
 *
 * Deliberately not an intersection with the DOM types: there, `id` and
 * `challenge` are already BufferSource, so intersecting makes every field both
 * string and buffer and the conversion below stops type-checking against
 * either. These are the wire shapes; the DOM types are what they become.
 */
interface CredentialDescriptorJson {
  id: string;
  type: string;
  transports?: string[];
}

interface CreationOptionsJson {
  challenge: string;
  rp: PublicKeyCredentialRpEntity;
  user: { id: string; name: string; displayName: string };
  pubKeyCredParams: PublicKeyCredentialParameters[];
  timeout?: number;
  attestation?: AttestationConveyancePreference;
  authenticatorSelection?: AuthenticatorSelectionCriteria;
  excludeCredentials?: CredentialDescriptorJson[];
}

interface RequestOptionsJson {
  challenge: string;
  timeout?: number;
  rpId?: string;
  userVerification?: UserVerificationRequirement;
  allowCredentials?: CredentialDescriptorJson[];
}

function toDescriptors(
  items: CredentialDescriptorJson[] | undefined,
): PublicKeyCredentialDescriptor[] {
  return (items ?? []).map((c) => ({
    id: base64urlToBuffer(c.id),
    type: 'public-key' as const,
    transports: c.transports as AuthenticatorTransport[] | undefined,
  }));
}

/** Server JSON → the shape `navigator.credentials.create` expects. */
export function toCreationOptions(
  options: Record<string, unknown>,
): PublicKeyCredentialCreationOptions {
  const o = options as unknown as CreationOptionsJson;
  return {
    rp: o.rp,
    pubKeyCredParams: o.pubKeyCredParams,
    timeout: o.timeout,
    attestation: o.attestation,
    authenticatorSelection: o.authenticatorSelection,
    challenge: base64urlToBuffer(o.challenge),
    user: {
      id: base64urlToBuffer(o.user.id),
      name: o.user.name,
      displayName: o.user.displayName,
    },
    excludeCredentials: toDescriptors(o.excludeCredentials),
  };
}

/** Server JSON → the shape `navigator.credentials.get` expects. */
export function toRequestOptions(
  options: Record<string, unknown>,
): PublicKeyCredentialRequestOptions {
  const o = options as unknown as RequestOptionsJson;
  return {
    challenge: base64urlToBuffer(o.challenge),
    timeout: o.timeout,
    rpId: o.rpId,
    userVerification: o.userVerification,
    allowCredentials: toDescriptors(o.allowCredentials),
  };
}

/** A new credential → JSON the backend can verify. */
export function registrationToJson(credential: PublicKeyCredential): unknown {
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
      attestationObject: bufferToBase64url(response.attestationObject),
    },
    transports: response.getTransports?.() ?? [],
  };
}

/** An assertion → JSON the backend can verify. */
export function assertionToJson(credential: PublicKeyCredential): unknown {
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bufferToBase64url(response.clientDataJSON),
      authenticatorData: bufferToBase64url(response.authenticatorData),
      signature: bufferToBase64url(response.signature),
      userHandle: response.userHandle
        ? bufferToBase64url(response.userHandle)
        : null,
    },
  };
}
