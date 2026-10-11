import crypto from "node:crypto";
import { fileBaseUrl, useSupabaseStore } from "./config.js";
import { engineCall } from "./engine-client.js";
import { ToolError } from "./errors.js";
import { getStore, type FileSummary } from "./store.js";
import { SupabaseStore } from "./supabase-store.js";
import { signToken } from "./tokens.js";

export function contentKey(ownerId: string, hash: string): string {
  return `${ownerId}/${hash}`;
}

export function incomingKey(ownerId: string, nonce: string): string {
  return `${ownerId}/incoming/${nonce}`;
}

export function stageKey(ownerId: string): string {
  return `${ownerId}/stage/${crypto.randomUUID()}`;
}

export function hostedEngine(): boolean {
  return Boolean(process.env.ENGINE_URL);
}

export function assertHostedStore(): SupabaseStore {
  const store = getStore();
  if (!hostedEngine() || !useSupabaseStore() || !(store instanceof SupabaseStore)) {
    throw new ToolError("ENGINE_URL requires the Supabase store so file bytes stay in Storage.", "bad_config");
  }
  return store;
}

export function assembleEnginePayload(input: {
  hosted: boolean;
  action: string;
  filename: string;
  bytes?: Buffer | null;
  extra?: Record<string, unknown>;
  sourceUrl?: string;
  sourceKey?: string;
  destUrl?: string;
  destKey?: string;
  storeSource?: boolean;
  beforeUrl?: string;
  beforeKey?: string;
  afterUrl?: string;
  afterKey?: string;
}): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    action: input.action,
    filename: input.filename,
    ...(input.extra ?? {})
  };
  if (!input.hosted) {
    if (input.bytes) payload.data_b64 = input.bytes.toString("base64");
    return payload;
  }
  if (input.sourceUrl) payload.source_url = input.sourceUrl;
  if (input.sourceKey) payload.source_key = input.sourceKey;
  if (input.destUrl) payload.dest_url = input.destUrl;
  if (input.destKey) payload.dest_key = input.destKey;
  if (input.storeSource) payload.store_source = true;
  if (input.beforeUrl) payload.before_url = input.beforeUrl;
  if (input.beforeKey) payload.before_key = input.beforeKey;
  if (input.afterUrl) payload.after_url = input.afterUrl;
  if (input.afterKey) payload.after_key = input.afterKey;
  return payload;
}

type StoredResult = { stored?: boolean; sha256?: string; size?: number };

export async function callFileEngine<T>(input: {
  ownerId: string;
  filename: string;
  action: string;
  sha?: string;
  bytes?: Buffer | null;
  extra?: Record<string, unknown>;
  write?: boolean;
  sourceUrl?: string;
  storeSource?: boolean;
}): Promise<T> {
  if (!hostedEngine()) {
    return engineCall<T>(
      assembleEnginePayload({
        hosted: false,
        action: input.action,
        filename: input.filename,
        bytes: input.bytes,
        extra: input.extra
      })
    );
  }
  const store = assertHostedStore();
  let sourceUrl = input.sourceUrl;
  let sourceKey: string | undefined;
  if (!sourceUrl && input.sha) {
    const signed = await store.signedDownload(contentKey(input.ownerId, input.sha));
    sourceUrl = signed.url;
    sourceKey = signed.key;
  }
  let stage: string | undefined;
  let destUrl: string | undefined;
  if (input.write || input.storeSource) {
    stage = stageKey(input.ownerId);
    const dest = await store.signedUpload(stage);
    destUrl = dest.url;
  }
  const result = await engineCall<T & StoredResult>(
    assembleEnginePayload({
      hosted: true,
      action: input.action,
      filename: input.filename,
      extra: input.extra,
      sourceUrl,
      sourceKey,
      destUrl,
      destKey: stage,
      storeSource: input.storeSource
    })
  );
  if (result.stored && result.sha256 && stage) {
    try {
      await store.copyObject(stage, contentKey(input.ownerId, result.sha256));
    } finally {
      await store.deleteObject(stage).catch(() => undefined);
    }
  }
  return result;
}

export async function verifyStoredPair(input: {
  ownerId: string;
  filename: string;
  beforeSha: string;
  afterSha: string;
  before?: Buffer;
  after?: Buffer;
}): Promise<Record<string, unknown>> {
  if (!hostedEngine()) {
    return engineCall(
      assembleEnginePayload({
        hosted: false,
        action: "verify",
        filename: input.filename,
        extra: {
          before_b64: input.before?.toString("base64"),
          after_b64: input.after?.toString("base64")
        }
      })
    );
  }
  const store = assertHostedStore();
  const before = await store.signedDownload(contentKey(input.ownerId, input.beforeSha));
  const after = await store.signedDownload(contentKey(input.ownerId, input.afterSha));
  return engineCall(
    assembleEnginePayload({
      hosted: true,
      action: "verify",
      filename: input.filename,
      beforeUrl: before.url,
      beforeKey: before.key,
      afterUrl: after.url,
      afterKey: after.key
    })
  );
}

export async function uploadLink(
  ownerId: string,
  reserved: { fileId: string; nonce: string; exp: number },
  filename: string
): Promise<{ upload_url: string; hosted: boolean }> {
  if (hostedEngine()) {
    const signed = await assertHostedStore().signedUpload(incomingKey(ownerId, reserved.nonce));
    return { upload_url: signed.url, hosted: true };
  }
  const token = signToken({
    typ: "put",
    fileId: reserved.fileId,
    ownerId,
    filename,
    exp: reserved.exp,
    nonce: reserved.nonce
  });
  return { upload_url: `${fileBaseUrl()}/v1/upload/${token}`, hosted: false };
}

export async function downloadLink(ownerId: string, fileId: string, sha: string, exp: number): Promise<string> {
  if (hostedEngine()) {
    const signed = await assertHostedStore().signedDownload(contentKey(ownerId, sha));
    return signed.url;
  }
  const token = signToken({ typ: "get", fileId, ownerId, sha, exp });
  return `${fileBaseUrl()}/v1/download/${token}`;
}

export async function completeHostedUpload(ownerId: string, fileId: string): Promise<FileSummary> {
  const store = assertHostedStore();
  const reserved = await store.getReservedUpload(ownerId, fileId);
  if (Date.parse(reserved.exp) <= Date.now()) {
    throw new ToolError("This upload link was already used or has expired.", "expired_link");
  }
  const source = await store.signedDownload(incomingKey(ownerId, reserved.nonce));
  const inspected = await callFileEngine<{ kind: string; sha256: string; size: number; stored?: boolean }>({
    ownerId,
    filename: reserved.filename,
    action: "inspect",
    sourceUrl: source.url,
    storeSource: true
  });
  if (!inspected.stored || !inspected.sha256) {
    throw new ToolError("The upload was not stored. The file was not saved.", "engine_error");
  }
  const saved = await store.commitStored({
    ownerId,
    filename: reserved.filename,
    kind: inspected.kind,
    sha: inspected.sha256,
    size: inspected.size,
    nonce: reserved.nonce
  });
  await store.deleteObject(incomingKey(ownerId, reserved.nonce)).catch(() => undefined);
  return saved;
}
