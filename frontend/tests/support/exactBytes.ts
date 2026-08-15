/** Exact-byte helpers the transport tests share: what the cockpit sent, byte for byte. */

export function base64Bytes(value: string): Uint8Array {
  return Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
}

export function bytesBase64(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes));
}

export function utf8Base64(value: string): string {
  return bytesBase64(new TextEncoder().encode(value));
}

/** The exact request body a journalled mutation carries, decoded as text. */
export function exactBody(mutation: { body_base64: string } | undefined): string {
  if (mutation === undefined) throw new Error("missing mutation");
  return new TextDecoder().decode(base64Bytes(mutation.body_base64));
}
