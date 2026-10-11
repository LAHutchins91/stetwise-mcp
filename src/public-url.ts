/** The public origin is whatever APP_BASE_URL this process was given. No legacy host is assumed. */
export function canonicalPublicOrigin(appBaseUrl: string) {
  return appBaseUrl.replace(/\/$/, "");
}

export function legacyMcpUrl(_appBaseUrl: string): string | null {
  return null;
}
