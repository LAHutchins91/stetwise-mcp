import { intEnv } from "./config.js";

/** Bytes kept in process for the current head and a proposal result. One blob larger than this is not cached. */
export const DEFAULT_HEAD_CACHE_BYTES = 64 * 1024 * 1024;

type Entry = { bytes: Buffer; expiresAt: number; size: number };

const entries = new Map<string, Entry>();
let total = 0;
let diskReads = 0;

export function headCacheMax(): number {
  return intEnv("STETWISE_HEAD_CACHE_BYTES", DEFAULT_HEAD_CACHE_BYTES);
}

export function headKey(ownerId: string, fileId: string, sha: string): string {
  return `${ownerId}:${fileId}:${sha}`;
}

export function noteDiskRead(): void {
  diskReads += 1;
}

export function diskReadCount(): number {
  return diskReads;
}

export function resetHeadCache(): void {
  entries.clear();
  total = 0;
  diskReads = 0;
}

export function headCacheClear(): void {
  entries.clear();
  total = 0;
}

export function headCacheGet(key: string, now = Date.now()): Buffer | undefined {
  const hit = entries.get(key);
  if (!hit) return undefined;
  if (hit.expiresAt <= now) {
    entries.delete(key);
    total -= hit.size;
    return undefined;
  }
  entries.delete(key);
  entries.set(key, hit);
  return hit.bytes;
}

export function headCacheTouch(key: string, expiresAt: string): void {
  const hit = entries.get(key);
  if (!hit) return;
  hit.expiresAt = Date.parse(expiresAt);
  entries.delete(key);
  entries.set(key, hit);
}

export function headCachePut(key: string, bytes: Buffer, expiresAt: string): void {
  const max = headCacheMax();
  if (bytes.length === 0 || bytes.length > max) return;
  const existing = entries.get(key);
  if (existing) {
    entries.delete(key);
    total -= existing.size;
  }
  while (total + bytes.length > max && entries.size > 0) {
    const oldest = entries.keys().next().value;
    if (!oldest) break;
    const evicted = entries.get(oldest);
    entries.delete(oldest);
    if (evicted) total -= evicted.size;
  }
  if (total + bytes.length > max) return;
  entries.set(key, { bytes, expiresAt: Date.parse(expiresAt), size: bytes.length });
  total += bytes.length;
}

function dropPrefix(prefix: string): void {
  for (const key of [...entries.keys()]) {
    if (!key.startsWith(prefix)) continue;
    const hit = entries.get(key);
    entries.delete(key);
    if (hit) total -= hit.size;
  }
}

export function headCacheDropFile(ownerId: string, fileId: string): void {
  dropPrefix(`${ownerId}:${fileId}:`);
}

export function headCacheDropOwner(ownerId: string): void {
  dropPrefix(`${ownerId}:`);
}
