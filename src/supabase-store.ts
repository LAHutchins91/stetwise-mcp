import crypto from "node:crypto";
import { supabaseServiceKey, supabaseUrl } from "./config.js";
import { ToolError } from "./errors.js";
import { headCacheClear, headCacheDropFile, headCacheDropOwner, headCacheGet, headCachePut, headCacheTouch, headKey, noteDiskRead } from "./head-cache.js";
import {
  emptyUsage,
  entitled,
  expiryFrom,
  FileTooLargeError,
  monthKey,
  normalizeRetention,
  planCaps,
  QuotaError,
  tierFor,
  usageUnits,
  usageView,
  type PlanTier,
  type UsageCounts,
  type UsageMetric
} from "./quota.js";
import {
  sha256,
  type FileRow,
  type FileSummary,
  type Profile,
  type ProposalRow,
  type PublishResult,
  type Store,
  type VersionSummary
} from "./store.js";

const BUCKET = "stetwise-files";

type Row = Record<string, unknown>;

function baseUrl(): string {
  const base = supabaseUrl();
  if (!base || !supabaseServiceKey()) throw new ToolError("Supabase is not configured.", "bad_config");
  return base;
}

function authHeaders(json = false): Headers {
  const key = supabaseServiceKey();
  const headers = new Headers();
  headers.set("apikey", key);
  headers.set("Authorization", `Bearer ${key}`);
  if (json) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/json");
  return headers;
}

function throwMapped(body: string): never {
  const found = body.match(/stetwise:[a-z0-9_:.]+/);
  const parts = (found?.[0] ?? "").split(":");
  const kind = parts[1] ?? "";
  if (kind === "usage_cap") {
    const metric = parts[2] as UsageMetric;
    throw new QuotaError(metric, Number(parts[3]), Number(parts[4]), parts[5] === "pro" ? "pro" : "trial");
  }
  if (kind === "file_too_large") {
    throw new FileTooLargeError(Number(parts[2]), Number(parts[3]), parts[4] === "pro" ? "pro" : "trial");
  }
  if (kind === "stale_version" && parts[2] === "proposal") {
    throw new ToolError("base_version does not match this proposal. Pass the token propose_edit returned.", "stale_version");
  }
  if (kind === "stale_version" && parts[2] === "propose") {
    throw new ToolError("base_version does not match the current file. Propose the edit again.", "stale_version");
  }
  if (kind === "stale_version") {
    throw new ToolError("base_version does not match the current file. List versions and propose the edit again.", "stale_version");
  }
  if (kind === "expired_link") throw new ToolError("This upload link was already used or has expired.", "expired_link");
  if (kind === "expired_file") throw new ToolError("This file has passed its retention window and can no longer be read.", "expired_file");
  if (kind === "expired") throw new ToolError("This proposal expired. Propose the edit again.", "expired");
  if (kind === "access") throw new ToolError("An active trial or Pro subscription is required.", "access");
  if (kind === "not_found" && parts[2] === "version") throw new ToolError("That version does not exist.", "not_found");
  if (kind === "not_found" && parts[2] === "proposal") throw new ToolError("Proposal not found.", "not_found");
  if (kind === "not_found" && parts[2] === "account") throw new ToolError("Account not found.", "not_found");
  if (kind === "not_found") throw new ToolError("File not found.", "not_found");
  if (kind === "bad_retention") throw new ToolError("Retention must be 24 hours or 168 hours (7 days).", "bad_retention");
  if (kind === "bad_hash") throw new ToolError("Bad content hash.", "bad_hash");
  if (body.includes("23503")) throw new ToolError("This account is not in the sign-in database yet.", "not_found");
  throw new ToolError("The stored account could not be updated.", "store");
}

async function rest<T>(method: string, pathName: string, body?: unknown, extra?: Record<string, string>): Promise<T> {
  const headers = authHeaders(body !== undefined && !(body instanceof Uint8Array) && !Buffer.isBuffer(body));
  if (extra) for (const [key, value] of Object.entries(extra)) headers.set(key, value);
  const payload = body === undefined ? undefined : Buffer.isBuffer(body) || body instanceof Uint8Array ? body : JSON.stringify(body);
  const response = await fetch(`${baseUrl()}${pathName}`, {
    method,
    headers,
    body: payload as BodyInit | undefined,
    signal: AbortSignal.timeout(120_000)
  });
  const text = await response.text();
  if (!response.ok) throwMapped(text || response.statusText);
  return (text ? JSON.parse(text) : null) as T;
}

async function rpc<T>(name: string, args: Record<string, unknown>): Promise<T> {
  return rest<T>("POST", `/rest/v1/rpc/${name}`, args);
}

function profileFrom(row: Row): Profile {
  return {
    id: String(row.id),
    email: row.email == null ? null : String(row.email),
    plan: String(row.plan ?? "trial"),
    subscription_status: String(row.subscription_status ?? "trialing"),
    retention_hours: normalizeRetention(Number(row.retention_hours ?? 24)),
    stripe_customer_id: row.stripe_customer_id == null ? null : String(row.stripe_customer_id),
    stripe_subscription_id: row.stripe_subscription_id == null ? null : String(row.stripe_subscription_id),
    current_period_end: row.current_period_end == null ? null : String(row.current_period_end),
    cancel_at_period_end: Boolean(row.cancel_at_period_end)
  };
}

function fileFrom(row: Row): FileRow {
  return {
    id: String(row.id),
    ownerId: String(row.owner_id),
    filename: String(row.filename),
    kind: String(row.kind),
    headSha: String(row.head_sha),
    size: Number(row.size),
    createdAt: String(row.created_at),
    lastUsedAt: String(row.last_used_at),
    expiresAt: String(row.expires_at)
  };
}

function summaryFrom(row: Row): FileSummary {
  return {
    file_id: String(row.file_id),
    filename: String(row.filename),
    kind: String(row.kind),
    size: Number(row.size),
    sha256: String(row.sha256),
    version: Number(row.version),
    created_at: String(row.created_at),
    last_used_at: String(row.last_used_at),
    expires_at: String(row.expires_at)
  };
}

function proposalFrom(row: Row): ProposalRow {
  return {
    id: String(row.id),
    ownerId: String(row.owner_id),
    fileId: String(row.file_id),
    baseSha: String(row.base_sha),
    resultSha: String(row.result_sha),
    size: Number(row.size),
    diff: row.diff,
    warnings: row.warnings,
    verification: row.verification,
    createdAt: String(row.created_at),
    expiresAt: String(row.expires_at),
    appliedAt: row.applied_at == null ? undefined : String(row.applied_at)
  };
}

function countsFrom(row: Row | null): UsageCounts {
  if (!row) return emptyUsage();
  return {
    edits: Number(row.edits ?? 0),
    files: Number(row.files ?? 0),
    recalculations: Number(row.recalculations ?? 0),
    proposals: Number(row.proposals ?? 0)
  };
}

function blobPath(ownerId: string, hash: string): string {
  if (!/^[a-f0-9]{64}$/.test(hash)) throw new ToolError("Bad content hash.", "bad_hash");
  return `${ownerId}/${hash}`;
}

const OBJECT_KEY = /^[0-9a-f-]{36}\/(?:[a-f0-9]{64}|incoming\/[a-f0-9]{32}|stage\/[0-9a-f-]{36})$/i;
const STAGING_GRACE_MS = 2 * 60 * 60 * 1000;

function encodeObjectKey(key: string): string {
  if (!OBJECT_KEY.test(key)) throw new ToolError("Bad storage key.", "bad_hash");
  return key.split("/").map(encodeURIComponent).join("/");
}

function absoluteStorageUrl(signedPath: string): string {
  if (signedPath.startsWith("http://") || signedPath.startsWith("https://")) return signedPath;
  const pathName = signedPath.startsWith("/") ? signedPath : `/${signedPath}`;
  return `${baseUrl()}/storage/v1${pathName}`;
}

function keepStaging(pathName: string, createdAt: string | undefined, now: Date): boolean {
  if (!pathName.includes("/incoming/") && !pathName.includes("/stage/")) return false;
  if (!createdAt) return true;
  const parsed = Date.parse(createdAt);
  if (Number.isNaN(parsed)) return true;
  return now.getTime() - parsed < STAGING_GRACE_MS;
}

export class SupabaseStore implements Store {
  private async uploadBlob(ownerId: string, hash: string, bytes: Buffer): Promise<void> {
    const headers = authHeaders(false);
    headers.set("Content-Type", "application/octet-stream");
    headers.set("x-upsert", "true");
    headers.set("cache-control", "no-store");
    const response = await fetch(`${baseUrl()}/storage/v1/object/${BUCKET}/${blobPath(ownerId, hash)}`, {
      method: "POST",
      headers,
      body: new Uint8Array(bytes),
      signal: AbortSignal.timeout(120_000)
    });
    if (!response.ok) throwMapped(await response.text());
  }

  private async downloadBlob(ownerId: string, hash: string): Promise<Buffer> {
    const response = await fetch(`${baseUrl()}/storage/v1/object/${BUCKET}/${blobPath(ownerId, hash)}`, {
      headers: authHeaders(false),
      signal: AbortSignal.timeout(120_000)
    });
    if (response.status === 404) throw new ToolError("The file bytes are no longer available.", "missing_bytes");
    if (!response.ok) throwMapped(await response.text());
    noteDiskRead();
    const bytes = Buffer.from(await response.arrayBuffer());
    if (sha256(bytes) !== hash) throw new ToolError("Stored bytes did not match their hash.", "bad_hash");
    return bytes;
  }

  private async cachedDownload(ownerId: string, fileId: string, hash: string, expiresAt: string): Promise<Buffer> {
    const key = headKey(ownerId, fileId, hash);
    const hit = headCacheGet(key);
    if (hit) {
      headCacheTouch(key, expiresAt);
      return hit;
    }
    const bytes = await this.downloadBlob(ownerId, hash);
    headCachePut(key, bytes, expiresAt);
    return bytes;
  }

  private async deleteBlobs(paths: string[]): Promise<number> {
    const unique = [...new Set(paths.filter(Boolean))];
    if (unique.length === 0) return 0;
    await rest("DELETE", `/storage/v1/object/${BUCKET}`, unique);
    return unique.length;
  }

  private async listBlobs(): Promise<{ path: string; createdAt?: string }[]> {
    const paths: { path: string; createdAt?: string }[] = [];
    const folders = await this.listPrefix("");
    for (const folder of folders) {
      if (folder.id) {
        paths.push({ path: folder.name, createdAt: folder.created_at || folder.updated_at });
        continue;
      }
      let offset = 0;
      for (;;) {
        const page = await this.listPrefix(`${folder.name}/`, offset);
        if (page.length === 0) break;
        for (const item of page) {
          paths.push({ path: `${folder.name}/${item.name}`, createdAt: item.created_at || item.updated_at });
        }
        if (page.length < 1000) break;
        offset += page.length;
      }
    }
    return paths;
  }

  private async listPrefix(prefix: string, offset = 0): Promise<{ name: string; id: string | null; created_at?: string; updated_at?: string }[]> {
    return rest("POST", `/storage/v1/object/list/${BUCKET}`, { prefix, limit: 1000, offset, sortBy: { column: "name", order: "asc" } });
  }

  async signedDownload(key: string, expiresIn = 900): Promise<{ url: string; key: string }> {
    const row = await rest<{ signedURL?: string; signedUrl?: string }>(
      "POST",
      `/storage/v1/object/sign/${BUCKET}/${encodeObjectKey(key)}`,
      { expiresIn }
    );
    const signed = row?.signedURL || row?.signedUrl || "";
    if (!signed) throw new ToolError("Storage did not return a download link.", "store");
    return { url: absoluteStorageUrl(signed), key };
  }

  async signedUpload(key: string): Promise<{ url: string; key: string }> {
    const row = await rest<{ url?: string; token?: string }>(
      "POST",
      `/storage/v1/object/upload/sign/${BUCKET}/${encodeObjectKey(key)}`,
      {}
    );
    let url = row?.url || "";
    const token = row?.token || "";
    if (!url && token) url = `/object/upload/sign/${BUCKET}/${key}`;
    if (!url) throw new ToolError("Storage did not return an upload link.", "store");
    url = absoluteStorageUrl(url);
    if (token && !url.includes("token=")) url += `${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
    return { url, key };
  }

  async copyObject(sourceKey: string, destinationKey: string): Promise<void> {
    encodeObjectKey(sourceKey);
    encodeObjectKey(destinationKey);
    const headers = authHeaders(true);
    headers.set("x-upsert", "true");
    const response = await fetch(`${baseUrl()}/storage/v1/object/copy`, {
      method: "POST",
      headers,
      body: JSON.stringify({ bucketId: BUCKET, sourceKey, destinationKey }),
      signal: AbortSignal.timeout(120_000)
    });
    if (response.ok) return;
    const text = await response.text();
    if ((response.status === 400 || response.status === 409) && /already exists|duplicate/i.test(text)) return;
    throwMapped(text);
  }

  async deleteObject(key: string): Promise<void> {
    encodeObjectKey(key);
    await this.deleteBlobs([key]);
  }

  async getReservedUpload(ownerId: string, fileId: string): Promise<{ nonce: string; filename: string; exp: string }> {
    const rows = await rest<Row[]>(
      "GET",
      `/rest/v1/upload_nonces?owner_id=eq.${encodeURIComponent(ownerId)}&file_id=eq.${encodeURIComponent(fileId)}&select=nonce,filename,exp&limit=1`
    );
    if (!rows[0]) throw new ToolError("This upload link was already used or has expired.", "expired_link");
    return { nonce: String(rows[0].nonce), filename: String(rows[0].filename), exp: String(rows[0].exp) };
  }

  async ensureProfile(input: { id: string; email?: string | null; plan?: string; subscription_status?: string }): Promise<Profile> {
    const existing = await this.getProfile(input.id);
    if (existing) return existing;
    try {
      const rows = await rest<Row[]>("POST", "/rest/v1/profiles", {
        id: input.id,
        email: input.email ?? null,
        plan: input.plan ?? "trial",
        subscription_status: input.subscription_status ?? "trialing",
        retention_hours: 24
      }, { Prefer: "return=representation" });
      return profileFrom(rows[0]);
    } catch (error) {
      if (error instanceof ToolError && error.code === "store") {
        const again = await this.getProfile(input.id);
        if (again) return again;
      }
      throw error;
    }
  }

  ensureDevProfile(): Promise<Profile> {
    return this.ensureProfile({
      id: "00000000-0000-4000-8000-000000000001",
      email: "dev@stetwise.local",
      plan: "trial",
      subscription_status: "trialing"
    });
  }

  async getProfile(id: string): Promise<Profile | null> {
    const rows = await rest<Row[]>("GET", `/rest/v1/profiles?id=eq.${encodeURIComponent(id)}&select=*`);
    return rows[0] ? profileFrom(rows[0]) : null;
  }

  async updateProfile(id: string, patch: Partial<Profile>): Promise<Profile> {
    const body: Row = {};
    const columns: Record<string, string> = {
      email: "email",
      plan: "plan",
      subscription_status: "subscription_status",
      retention_hours: "retention_hours",
      stripe_customer_id: "stripe_customer_id",
      stripe_subscription_id: "stripe_subscription_id",
      current_period_end: "current_period_end",
      cancel_at_period_end: "cancel_at_period_end"
    };
    for (const [key, column] of Object.entries(columns)) {
      if (key in patch) body[column] = patch[key as keyof Profile];
    }
    body.updated_at = new Date().toISOString();
    const rows = await rest<Row[]>("PATCH", `/rest/v1/profiles?id=eq.${encodeURIComponent(id)}`, body, { Prefer: "return=representation" });
    if (!rows[0]) throw new ToolError("Account not found.", "not_found");
    return profileFrom(rows[0]);
  }

  async usage(ownerId: string, now = new Date()) {
    const profile = await this.requireProfile(ownerId);
    const rows = await rest<Row[]>(
      "GET",
      `/rest/v1/usage_monthly?owner_id=eq.${encodeURIComponent(ownerId)}&month=eq.${monthKey(now)}&select=edits,files,recalculations,proposals`
    );
    return usageView(tierFor(profile), countsFrom(rows[0] ?? null), profile.retention_hours, now);
  }

  async replaceUsage(ownerId: string, counts: Partial<UsageCounts>, now = new Date()): Promise<void> {
    const month = monthKey(now);
    const rows = await rest<Row[]>(
      "GET",
      `/rest/v1/usage_monthly?owner_id=eq.${encodeURIComponent(ownerId)}&month=eq.${month}&select=edits,files,recalculations,proposals`
    );
    const current = countsFrom(rows[0] ?? null);
    await rest("POST", "/rest/v1/usage_monthly", {
      owner_id: ownerId,
      month,
      edits: counts.edits ?? current.edits,
      files: counts.files ?? current.files,
      recalculations: counts.recalculations ?? current.recalculations,
      proposals: counts.proposals ?? current.proposals
    }, { Prefer: "resolution=merge-duplicates", on_conflict: "owner_id,month" });
  }

  async setRetention(ownerId: string, hours: 24 | 168): Promise<Profile> {
    const row = await rpc<Row>("stetwise_set_retention", { p_owner: ownerId, p_hours: hours });
    return profileFrom(row);
  }

  async reserveUpload(ownerId: string, filename: string, now = new Date()) {
    const profile = await this.requireEntitled(ownerId);
    const tier = tierFor(profile);
    const fileId = crypto.randomUUID();
    const nonce = crypto.randomBytes(16).toString("hex");
    const exp = Math.floor(now.getTime() / 1000) + 15 * 60;
    await rpc("stetwise_reserve_upload", {
      p_owner: ownerId,
      p_file_id: fileId,
      p_nonce: nonce,
      p_filename: filename,
      p_exp: new Date(exp * 1000).toISOString(),
      p_month: monthKey(now),
      p_files_cap: planCaps(tier).files,
      p_tier: tier
    });
    return { fileId, nonce, exp };
  }

  async commitNewFile(input: {
    ownerId: string;
    filename: string;
    kind: string;
    bytes: Buffer;
    nonce?: string;
    now?: Date;
  }): Promise<FileSummary> {
    const now = input.now ?? new Date();
    const profile = await this.requireEntitled(input.ownerId);
    const tier = tierFor(profile);
    const caps = planCaps(tier);
    if (input.bytes.length > caps.maxFileBytes) throw new FileTooLargeError(input.bytes.length, caps.maxFileBytes, tier);
    const hash = sha256(input.bytes);
    await this.uploadBlob(input.ownerId, hash, input.bytes);
    return this.commitStored({
      ownerId: input.ownerId,
      filename: input.filename,
      kind: input.kind,
      sha: hash,
      size: input.bytes.length,
      nonce: input.nonce,
      now
    });
  }

  async commitStored(input: {
    ownerId: string;
    filename: string;
    kind: string;
    sha: string;
    size: number;
    nonce?: string;
    now?: Date;
  }): Promise<FileSummary> {
    const now = input.now ?? new Date();
    const profile = await this.requireEntitled(input.ownerId);
    const tier = tierFor(profile);
    const caps = planCaps(tier);
    if (!/^[a-f0-9]{64}$/.test(input.sha)) throw new ToolError("Bad content hash.", "bad_hash");
    if (input.size > caps.maxFileBytes) throw new FileTooLargeError(input.size, caps.maxFileBytes, tier);
    const row = await rpc<Row>("stetwise_commit_file", {
      p_owner: input.ownerId,
      p_file_id: crypto.randomUUID(),
      p_filename: input.filename,
      p_kind: input.kind,
      p_sha: input.sha,
      p_size: input.size,
      p_nonce: input.nonce ?? "",
      p_month: monthKey(now),
      p_files_cap: caps.files,
      p_max_bytes: caps.maxFileBytes,
      p_tier: tier,
      p_now: now.toISOString()
    });
    return summaryFrom(row);
  }

  async listFiles(ownerId: string, now = new Date()): Promise<FileSummary[]> {
    const rows = await rest<Row[]>(
      "GET",
      `/rest/v1/files?owner_id=eq.${encodeURIComponent(ownerId)}&expires_at=gt.${encodeURIComponent(now.toISOString())}&select=id,filename,kind,head_sha,size,created_at,last_used_at,expires_at,file_versions(number)&order=created_at.desc`
    );
    return rows.map((row) => {
      const versions = (row.file_versions as Row[] | undefined) ?? [];
      const version = versions.reduce((max, item) => Math.max(max, Number(item.number)), 0);
      return {
        file_id: String(row.id),
        filename: String(row.filename),
        kind: String(row.kind),
        size: Number(row.size),
        sha256: String(row.head_sha),
        version: version || 1,
        created_at: String(row.created_at),
        last_used_at: String(row.last_used_at),
        expires_at: String(row.expires_at)
      };
    });
  }

  async getFile(ownerId: string, fileId: string): Promise<FileSummary> {
    const file = await this.liveFile(ownerId, fileId);
    const touched = await this.touch(file);
    const versions = await this.listVersions(ownerId, fileId);
    return {
      file_id: touched.id,
      filename: touched.filename,
      kind: touched.kind,
      size: touched.size,
      sha256: touched.headSha,
      version: versions[versions.length - 1]?.version ?? 1,
      created_at: touched.createdAt,
      last_used_at: touched.lastUsedAt,
      expires_at: touched.expiresAt
    };
  }

  async readOwnedBlob(ownerId: string, fileId: string, hash: string): Promise<{ bytes: Buffer; file: FileRow }> {
    const file = await this.liveFile(ownerId, fileId);
    const versions = await rest<Row[]>(
      "GET",
      `/rest/v1/file_versions?file_id=eq.${encodeURIComponent(file.id)}&sha256=eq.${hash}&select=number&limit=1`
    );
    const now = new Date().toISOString();
    const proposals = await rest<Row[]>(
      "GET",
      `/rest/v1/proposals?owner_id=eq.${encodeURIComponent(ownerId)}&file_id=eq.${encodeURIComponent(file.id)}&result_sha=eq.${hash}&expires_at=gt.${encodeURIComponent(now)}&select=id&limit=1`
    );
    if (versions.length === 0 && proposals.length === 0) throw new ToolError("That version is not part of this file.", "not_found");
    return { bytes: await this.cachedDownload(ownerId, file.id, hash, file.expiresAt), file };
  }

  async readBytes(ownerId: string, fileId: string, hash?: string): Promise<{ bytes: Buffer; file: FileRow }> {
    const file = await this.liveFile(ownerId, fileId);
    const sha = hash ?? file.headSha;
    const known = await rest<Row[]>(
      "GET",
      `/rest/v1/file_versions?file_id=eq.${encodeURIComponent(file.id)}&sha256=eq.${sha}&select=number&limit=1`
    );
    if (known.length === 0) throw new ToolError("That version is not part of this file.", "not_found");
    const touched = await this.touch(file);
    return { bytes: await this.cachedDownload(ownerId, touched.id, sha, touched.expiresAt), file: touched };
  }

  async listVersions(ownerId: string, fileId: string): Promise<VersionSummary[]> {
    const file = await this.liveFile(ownerId, fileId);
    await this.touch(file);
    const rows = await rest<Row[]>(
      "GET",
      `/rest/v1/file_versions?file_id=eq.${encodeURIComponent(file.id)}&select=number,sha256,size,op,created_at&order=number.asc`
    );
    return rows.map((row) => ({
      version: Number(row.number),
      sha256: String(row.sha256),
      size: Number(row.size),
      op: String(row.op) as VersionSummary["op"],
      created_at: String(row.created_at)
    }));
  }

  async saveProposal(input: {
    ownerId: string;
    fileId: string;
    baseSha: string;
    resultSha: string;
    size: number;
    bytes: Buffer;
    diff: unknown;
    warnings: unknown;
    verification: unknown;
    stored?: boolean;
  }): Promise<ProposalRow> {
    if (input.stored) {
      if (!/^[a-f0-9]{64}$/.test(input.resultSha)) throw new ToolError("Bad content hash.", "bad_hash");
    } else if (sha256(input.bytes) !== input.resultSha) {
      throw new ToolError("Bad content hash.", "bad_hash");
    }
    const profile = await this.requireEntitled(input.ownerId);
    const tier = tierFor(profile);
    const file = await this.liveFile(input.ownerId, input.fileId);
    const now = new Date();
    const units = usageUnits(file.size);
    const view = await this.usage(input.ownerId, now);
    if (view.proposals.used + units > view.proposals.cap) {
      throw new QuotaError("proposals", view.proposals.used, view.proposals.cap, tier, now, units);
    }
    if (!input.stored) await this.uploadBlob(input.ownerId, input.resultSha, input.bytes);
    const row = await rpc<Row>("stetwise_save_proposal", {
      p_id: crypto.randomUUID(),
      p_owner: input.ownerId,
      p_file_id: input.fileId,
      p_base_sha: input.baseSha,
      p_result_sha: input.resultSha,
      p_size: input.size,
      p_diff: input.diff ?? [],
      p_warnings: input.warnings ?? [],
      p_verification: input.verification ?? {},
      p_now: now.toISOString(),
      p_month: monthKey(now),
      p_proposals_cap: planCaps(tier).proposals,
      p_tier: tier
    });
    const proposal = proposalFrom(row);
    if (!input.stored) headCachePut(headKey(input.ownerId, file.id, input.resultSha), input.bytes, file.expiresAt);
    return proposal;
  }

  async getProposal(ownerId: string, proposalId: string): Promise<ProposalRow> {
    const rows = await rest<Row[]>(
      "GET",
      `/rest/v1/proposals?id=eq.${encodeURIComponent(proposalId)}&owner_id=eq.${encodeURIComponent(ownerId)}&select=*`
    );
    if (!rows[0]) throw new ToolError("Proposal not found.", "not_found");
    return proposalFrom(rows[0]);
  }

  async publishEdit(ownerId: string, proposalId: string, baseVersion: string, now = new Date()): Promise<PublishResult> {
    const profile = await this.requireEntitled(ownerId);
    const tier = tierFor(profile);
    const row = await rpc<Row>("stetwise_publish_edit", {
      p_owner: ownerId,
      p_proposal: proposalId,
      p_base: baseVersion,
      p_month: monthKey(now),
      p_edits_cap: planCaps(tier).edits,
      p_tier: tier,
      p_now: now.toISOString()
    });
    return this.publishFrom(row, tier, now);
  }

  async recordRecalculation(ownerId: string, now = new Date(), sizeBytes = 1) {
    const profile = await this.requireEntitled(ownerId);
    const tier = tierFor(profile);
    const row = await rpc<Row>("stetwise_record_recalculation", {
      p_owner: ownerId,
      p_month: monthKey(now),
      p_cap: planCaps(tier).recalculations,
      p_tier: tier,
      p_units: usageUnits(sizeBytes)
    });
    return usageView(tier, countsFrom(row), profile.retention_hours, now);
  }

  async assertRecalcAllowed(ownerId: string, now = new Date(), sizeBytes = 1): Promise<void> {
    const profile = await this.requireEntitled(ownerId);
    const view = await this.usage(ownerId, now);
    const units = usageUnits(sizeBytes);
    if (view.recalculations.used + units > view.recalculations.cap) {
      throw new QuotaError("recalculations", view.recalculations.used, view.recalculations.cap, tierFor(profile), now, units);
    }
  }

  async restore(ownerId: string, fileId: string, version: number, now = new Date()): Promise<PublishResult> {
    const profile = await this.requireEntitled(ownerId);
    const tier = tierFor(profile);
    const row = await rpc<Row>("stetwise_restore", {
      p_owner: ownerId,
      p_file_id: fileId,
      p_version: version,
      p_now: now.toISOString()
    });
    return this.publishFrom(row, tier, now);
  }

  async deleteFile(ownerId: string, fileId: string): Promise<void> {
    const rows = await rest<Row[]>(
      "DELETE",
      `/rest/v1/files?id=eq.${encodeURIComponent(fileId)}&owner_id=eq.${encodeURIComponent(ownerId)}`,
      undefined,
      { Prefer: "return=representation" }
    );
    if (!rows?.length) throw new ToolError("File not found.", "not_found");
    headCacheDropFile(ownerId, fileId);
    await this.audit({ ownerId, op: "delete_file", fileId, code: "ok" });
  }

  async deleteAll(ownerId: string): Promise<number> {
    const rows = await rest<Row[]>("DELETE", `/rest/v1/files?owner_id=eq.${encodeURIComponent(ownerId)}`, undefined, {
      Prefer: "return=representation"
    });
    headCacheDropOwner(ownerId);
    await this.audit({ ownerId, op: "delete_all", code: "ok" });
    return rows?.length ?? 0;
  }

  async purge(now = new Date()): Promise<{ files: number; blobs: number }> {
    const row = await rpc<Row>("stetwise_purge", { p_now: now.toISOString() });
    const drop = (Array.isArray(row.drop) ? row.drop : []) as Row[];
    const keep = new Set((Array.isArray(row.keep) ? row.keep : []).map((item) => {
      const ref = item as Row;
      return `${ref.owner_id}/${ref.sha256}`;
    }));
    const paths = drop.map((item) => `${item.owner_id}/${item.sha256}`);
    try {
      const listed = await this.listBlobs();
      for (const item of listed) {
        if (keep.has(item.path) || keepStaging(item.path, item.createdAt, now)) continue;
        paths.push(item.path);
      }
    } catch {
      // The explicit drop list still removes blobs the database released.
    }
    const blobs = await this.deleteBlobs(paths);
    headCacheClear();
    return { files: Number(row.files ?? 0), blobs };
  }

  async audit(event: { ownerId: string; op: string; fileId?: string; sha256?: string; size?: number; code: string }): Promise<void> {
    const owner = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(event.ownerId) ? event.ownerId : null;
    await rest("POST", "/rest/v1/audit_events", {
      owner_id: owner,
      op: event.op,
      file_id: event.fileId ?? null,
      sha256: event.sha256 ?? null,
      size: event.size ?? null,
      code: event.code
    }, { Prefer: "return=minimal" });
  }

  async support(email: string, message: string): Promise<{ id: string }> {
    const id = crypto.randomUUID();
    await rest("POST", "/rest/v1/support_requests", { id, email, message }, { Prefer: "return=minimal" });
    await rest("POST", "/rest/v1/audit_events", { owner_id: null, op: "support", code: "ok" }, { Prefer: "return=minimal" });
    return { id };
  }

  async expireFile(fileId: string, at: string): Promise<void> {
    const rows = await rest<Row[]>("PATCH", `/rest/v1/files?id=eq.${encodeURIComponent(fileId)}`, { expires_at: at }, {
      Prefer: "return=representation"
    });
    if (!rows[0]) throw new ToolError("File not found.", "not_found");
  }

  private async requireProfile(ownerId: string): Promise<Profile> {
    const profile = await this.getProfile(ownerId);
    if (!profile) throw new ToolError("Account not found.", "not_found");
    return profile;
  }

  private async requireEntitled(ownerId: string): Promise<Profile> {
    const profile = await this.requireProfile(ownerId);
    if (!entitled(profile)) throw new ToolError("An active trial or Pro subscription is required.", "access");
    return profile;
  }

  private async liveFile(ownerId: string, fileId: string, now = new Date()): Promise<FileRow> {
    const rows = await rest<Row[]>(
      "GET",
      `/rest/v1/files?id=eq.${encodeURIComponent(fileId)}&owner_id=eq.${encodeURIComponent(ownerId)}&select=*,profiles(retention_hours)`
    );
    if (!rows[0]) throw new ToolError("File not found.", "not_found");
    const file = fileFrom(rows[0]);
    if (Date.parse(file.expiresAt) <= now.getTime()) {
      throw new ToolError("This file has passed its retention window and can no longer be read.", "expired_file");
    }
    const embedded = rows[0].profiles as Row | Row[] | undefined;
    const profileRow = Array.isArray(embedded) ? embedded[0] : embedded;
    fileRowRetention.set(file.id, normalizeRetention(Number(profileRow?.retention_hours ?? 24)));
    return file;
  }

  private async touch(file: FileRow, now = new Date()): Promise<FileRow> {
    const hours = fileRowRetention.get(file.id) ?? 24;
    const lastUsedAt = now.toISOString();
    const expiresAt = expiryFrom(lastUsedAt, hours);
    await rest("PATCH", `/rest/v1/files?id=eq.${encodeURIComponent(file.id)}`, {
      last_used_at: lastUsedAt,
      expires_at: expiresAt
    }, { Prefer: "return=minimal" });
    return { ...file, lastUsedAt, expiresAt };
  }

  private publishFrom(row: Row, tier: PlanTier, now: Date): PublishResult {
    return {
      file_id: String(row.file_id),
      version: Number(row.version),
      sha256: String(row.sha256),
      size: Number(row.size),
      idempotent: Boolean(row.idempotent),
      usage: usageView(tier, countsFrom(row), normalizeRetention(Number(row.retention_hours ?? 24)), now)
    };
  }
}

const fileRowRetention = new Map<string, 24 | 168>();
