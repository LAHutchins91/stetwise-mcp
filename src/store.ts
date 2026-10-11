import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { dataDir, DEV_USER_ID, useSupabaseStore } from "./config.js";
import { SupabaseStore } from "./supabase-store.js";
import { ToolError } from "./errors.js";
import {
  headCacheDropFile,
  headCacheDropOwner,
  headCacheGet,
  headCachePut,
  headCacheTouch,
  headKey,
  noteDiskRead,
  resetHeadCache
} from "./head-cache.js";
import {
  emptyUsage,
  entitled,
  expiryFrom,
  FileTooLargeError,
  monthKey,
  planCaps,
  QuotaError,
  tierFor,
  usageUnits,
  usageView,
  type PlanTier,
  type UsageCounts,
  type UsageMetric
} from "./quota.js";

export type Profile = {
  id: string;
  email: string | null;
  plan: string;
  subscription_status: string;
  retention_hours: 24 | 168;
  stripe_customer_id: string | null;
  stripe_subscription_id: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
};

export type FileRow = {
  id: string;
  ownerId: string;
  filename: string;
  kind: string;
  headSha: string;
  size: number;
  createdAt: string;
  lastUsedAt: string;
  expiresAt: string;
};

export type VersionRow = {
  fileId: string;
  number: number;
  sha256: string;
  size: number;
  op: "import" | "apply" | "restore";
  createdAt: string;
};

export type ProposalRow = {
  id: string;
  ownerId: string;
  fileId: string;
  baseSha: string;
  resultSha: string;
  size: number;
  diff: unknown;
  warnings: unknown;
  verification: unknown;
  createdAt: string;
  expiresAt: string;
  appliedAt?: string;
};

type UploadRow = {
  nonce: string;
  ownerId: string;
  fileId: string;
  filename: string;
  exp: number;
};

type State = {
  profiles: Record<string, Profile>;
  files: Record<string, FileRow>;
  versions: VersionRow[];
  proposals: Record<string, ProposalRow>;
  uploads: Record<string, UploadRow>;
  usage: Record<string, Record<string, UsageCounts>>;
};

export type FileSummary = {
  file_id: string;
  filename: string;
  kind: string;
  size: number;
  sha256: string;
  version: number;
  created_at: string;
  last_used_at: string;
  expires_at: string;
};

export type PublishResult = {
  file_id: string;
  version: number;
  sha256: string;
  size: number;
  idempotent: boolean;
  usage: ReturnType<typeof usageView>;
};

export type VersionSummary = {
  version: number;
  sha256: string;
  size: number;
  op: VersionRow["op"];
  created_at: string;
};

export interface Store {
  ensureProfile(input: { id: string; email?: string | null; plan?: string; subscription_status?: string }): Promise<Profile>;
  ensureDevProfile(): Promise<Profile>;
  getProfile(id: string): Promise<Profile | null>;
  updateProfile(id: string, patch: Partial<Profile>): Promise<Profile>;
  usage(ownerId: string, now?: Date): Promise<ReturnType<typeof usageView>>;
  replaceUsage(ownerId: string, counts: Partial<UsageCounts>, now?: Date): Promise<void>;
  setRetention(ownerId: string, hours: 24 | 168): Promise<Profile>;
  reserveUpload(ownerId: string, filename: string, now?: Date): Promise<{ fileId: string; nonce: string; exp: number }>;
  commitNewFile(input: {
    ownerId: string;
    filename: string;
    kind: string;
    bytes: Buffer;
    nonce?: string;
    now?: Date;
  }): Promise<FileSummary>;
  listFiles(ownerId: string, now?: Date): Promise<FileSummary[]>;
  getFile(ownerId: string, fileId: string): Promise<FileSummary>;
  readOwnedBlob(ownerId: string, fileId: string, hash: string): Promise<{ bytes: Buffer; file: FileRow }>;
  readBytes(ownerId: string, fileId: string, hash?: string): Promise<{ bytes: Buffer; file: FileRow }>;
  listVersions(ownerId: string, fileId: string): Promise<VersionSummary[]>;
  saveProposal(input: {
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
  }): Promise<ProposalRow>;
  getProposal(ownerId: string, proposalId: string): Promise<ProposalRow>;
  publishEdit(ownerId: string, proposalId: string, baseVersion: string, now?: Date): Promise<PublishResult>;
  recordRecalculation(ownerId: string, now?: Date, sizeBytes?: number): Promise<ReturnType<typeof usageView>>;
  assertRecalcAllowed(ownerId: string, now?: Date, sizeBytes?: number): Promise<void>;
  restore(ownerId: string, fileId: string, version: number, now?: Date): Promise<PublishResult>;
  deleteFile(ownerId: string, fileId: string): Promise<void>;
  deleteAll(ownerId: string): Promise<number>;
  purge(now?: Date): Promise<{ files: number; blobs: number }>;
  audit(event: { ownerId: string; op: string; fileId?: string; sha256?: string; size?: number; code: string }): Promise<void>;
  support(email: string, message: string): Promise<{ id: string }>;
  expireFile(fileId: string, at: string): Promise<void>;
}

const EMPTY = (): State => ({ profiles: {}, files: {}, versions: [], proposals: {}, uploads: {}, usage: {} });

let chain: Promise<unknown> = Promise.resolve();
let cached: Store | undefined;

function locked<T>(fn: () => T): Promise<T> {
  const run = chain.then(() => fn());
  chain = run.then(
    () => undefined,
    () => undefined
  );
  return run;
}

function statePath(): string {
  return path.join(dataDir(), "state.json");
}

function load(): State {
  try {
    return JSON.parse(fs.readFileSync(statePath(), "utf8")) as State;
  } catch {
    return EMPTY();
  }
}

function save(state: State): void {
  fs.mkdirSync(dataDir(), { recursive: true });
  const tmp = `${statePath()}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(state));
  fs.renameSync(tmp, statePath());
}

export function sha256(bytes: Buffer): string {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function blobPath(hash: string): string {
  if (!/^[a-f0-9]{64}$/.test(hash)) throw new ToolError("Bad content hash.", "bad_hash");
  return path.join(dataDir(), "blobs", hash);
}

function writeBlob(hash: string, bytes: Buffer): void {
  const file = blobPath(hash);
  if (fs.existsSync(file)) return;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, bytes);
  fs.renameSync(tmp, file);
}

function readBlob(hash: string): Buffer {
  try {
    noteDiskRead();
    return fs.readFileSync(blobPath(hash));
  } catch {
    throw new ToolError("The file bytes are no longer available.", "missing_bytes");
  }
}

function cachedBytes(ownerId: string, fileId: string, sha: string, expiresAt: string, now: number): Buffer {
  const key = headKey(ownerId, fileId, sha);
  const hit = headCacheGet(key, now);
  if (hit) {
    headCacheTouch(key, expiresAt);
    return hit;
  }
  const bytes = readBlob(sha);
  headCachePut(key, bytes, expiresAt);
  return bytes;
}

function countsOf(state: State, ownerId: string, now: Date): UsageCounts {
  return state.usage[ownerId]?.[monthKey(now)] ?? emptyUsage();
}

function bump(state: State, ownerId: string, metric: UsageMetric, tier: PlanTier, now: Date, units = 1): UsageCounts {
  if (!Number.isInteger(units) || units < 1) {
    throw new ToolError("Usage weight must be a positive whole number.", "bad_request");
  }
  const month = monthKey(now);
  state.usage[ownerId] ??= {};
  const current = state.usage[ownerId][month] ?? emptyUsage();
  const cap = planCaps(tier)[metric];
  if (current[metric] + units > cap) throw new QuotaError(metric, current[metric], cap, tier, now, units);
  current[metric] += units;
  state.usage[ownerId][month] = current;
  return current;
}

function liveFile(state: State, ownerId: string, fileId: string, now = new Date()): FileRow {
  const file = state.files[fileId];
  if (!file || file.ownerId !== ownerId) throw new ToolError("File not found.", "not_found");
  if (Date.parse(file.expiresAt) <= now.getTime()) {
    headCacheDropFile(file.ownerId, fileId);
    throw new ToolError("This file has passed its retention window and can no longer be read.", "expired_file");
  }
  return file;
}

function touch(state: State, file: FileRow, now: Date): void {
  const profile = state.profiles[file.ownerId];
  file.lastUsedAt = now.toISOString();
  file.expiresAt = expiryFrom(file.lastUsedAt, profile?.retention_hours ?? 24);
}

function headVersion(state: State, file: FileRow): number {
  return state.versions.filter((version) => version.fileId === file.id).reduce((max, version) => Math.max(max, version.number), 0);
}

function summary(state: State, file: FileRow): FileSummary {
  return {
    file_id: file.id,
    filename: file.filename,
    kind: file.kind,
    size: file.size,
    sha256: file.headSha,
    version: headVersion(state, file),
    created_at: file.createdAt,
    last_used_at: file.lastUsedAt,
    expires_at: file.expiresAt
  };
}

function appendAudit(event: { ownerId: string; op: string; fileId?: string; sha256?: string; size?: number; code: string }): void {
  fs.mkdirSync(dataDir(), { recursive: true });
  const line = JSON.stringify({ ...event, at: new Date().toISOString() });
  fs.appendFileSync(path.join(dataDir(), "audit.jsonl"), `${line}\n`);
}

function referenced(state: State, now: Date): Set<string> {
  const shas = new Set<string>();
  for (const version of state.versions) {
    if (state.files[version.fileId]) shas.add(version.sha256);
  }
  for (const proposal of Object.values(state.proposals)) {
    if (!state.files[proposal.fileId]) continue;
    if (proposal.appliedAt || Date.parse(proposal.expiresAt) > now.getTime()) shas.add(proposal.resultSha);
  }
  return shas;
}

export class LocalStore implements Store {
  ensureProfile(input: { id: string; email?: string | null; plan?: string; subscription_status?: string }): Promise<Profile> {
    return locked(() => {
      const state = load();
      const existing = state.profiles[input.id];
      if (existing) return existing;
      const profile: Profile = {
        id: input.id,
        email: input.email ?? null,
        plan: input.plan ?? "trial",
        subscription_status: input.subscription_status ?? "trialing",
        retention_hours: 24,
        stripe_customer_id: null,
        stripe_subscription_id: null,
        current_period_end: null,
        cancel_at_period_end: false
      };
      state.profiles[input.id] = profile;
      save(state);
      return profile;
    });
  }

  ensureDevProfile(): Promise<Profile> {
    return this.ensureProfile({
      id: DEV_USER_ID,
      email: "dev@stetwise.local",
      plan: "trial",
      subscription_status: "trialing"
    });
  }

  getProfile(id: string): Promise<Profile | null> {
    return locked(() => load().profiles[id] ?? null);
  }

  updateProfile(id: string, patch: Partial<Profile>): Promise<Profile> {
    return locked(() => {
      const state = load();
      const profile = state.profiles[id];
      if (!profile) throw new ToolError("Account not found.", "not_found");
      Object.assign(profile, patch, { id });
      save(state);
      return profile;
    });
  }

  usage(ownerId: string, now = new Date()) {
    return locked(() => {
      const state = load();
      const profile = state.profiles[ownerId];
      if (!profile) throw new ToolError("Account not found.", "not_found");
      return usageView(tierFor(profile), countsOf(state, ownerId, now), profile.retention_hours, now);
    });
  }

  replaceUsage(ownerId: string, counts: Partial<UsageCounts>, now = new Date()): Promise<void> {
    return locked(() => {
      const state = load();
      const month = monthKey(now);
      state.usage[ownerId] ??= {};
      state.usage[ownerId][month] = { ...emptyUsage(), ...state.usage[ownerId][month], ...counts };
      save(state);
    });
  }

  setRetention(ownerId: string, hours: 24 | 168): Promise<Profile> {
    return locked(() => {
      const state = load();
      const profile = state.profiles[ownerId];
      if (!profile) throw new ToolError("Account not found.", "not_found");
      if (!entitled(profile)) throw new ToolError("An active trial or Pro subscription is required.", "access");
      profile.retention_hours = hours;
      for (const file of Object.values(state.files)) {
        if (file.ownerId === ownerId) file.expiresAt = expiryFrom(file.lastUsedAt, hours);
      }
      save(state);
      return profile;
    });
  }

  reserveUpload(ownerId: string, filename: string, now = new Date()): Promise<{ fileId: string; nonce: string; exp: number }> {
    return locked(() => {
      const state = load();
      const profile = requireEntitled(state, ownerId);
      const counts = countsOf(state, ownerId, now);
      const caps = planCaps(tierFor(profile));
      if (counts.files >= caps.files) throw new QuotaError("files", counts.files, caps.files, tierFor(profile), now);
      const fileId = crypto.randomUUID();
      const nonce = crypto.randomBytes(16).toString("hex");
      const exp = Math.floor(now.getTime() / 1000) + 15 * 60;
      state.uploads[nonce] = { nonce, ownerId, fileId, filename, exp };
      save(state);
      return { fileId, nonce, exp };
    });
  }

  commitNewFile(input: {
    ownerId: string;
    filename: string;
    kind: string;
    bytes: Buffer;
    nonce?: string;
    now?: Date;
  }): Promise<FileSummary> {
    return locked(() => {
      const now = input.now ?? new Date();
      const state = load();
      const profile = requireEntitled(state, input.ownerId);
      const tier = tierFor(profile);
      const caps = planCaps(tier);
      if (input.bytes.length > caps.maxFileBytes) {
        throw new FileTooLargeError(input.bytes.length, caps.maxFileBytes, tier);
      }
      let fileId: string = crypto.randomUUID();
      let filename = input.filename;
      if (input.nonce) {
        const upload = state.uploads[input.nonce];
        if (!upload || upload.ownerId !== input.ownerId || upload.exp <= Math.floor(now.getTime() / 1000)) {
          throw new ToolError("This upload link was already used or has expired.", "expired_link");
        }
        fileId = upload.fileId;
        filename = upload.filename;
      }
      const hash = sha256(input.bytes);
      bump(state, input.ownerId, "files", tier, now, usageUnits(input.bytes.length));
      if (input.nonce) delete state.uploads[input.nonce];
      writeBlob(hash, input.bytes);
      const createdAt = now.toISOString();
      const row: FileRow = {
        id: fileId,
        ownerId: input.ownerId,
        filename,
        kind: input.kind,
        headSha: hash,
        size: input.bytes.length,
        createdAt,
        lastUsedAt: createdAt,
        expiresAt: expiryFrom(createdAt, profile.retention_hours)
      };
      state.files[row.id] = row;
      state.versions.push({ fileId: row.id, number: 1, sha256: hash, size: input.bytes.length, op: "import", createdAt });
      save(state);
      appendAudit({ ownerId: input.ownerId, op: "import", fileId: row.id, sha256: hash, size: input.bytes.length, code: "ok" });
      return summary(state, row);
    });
  }

  listFiles(ownerId: string, now = new Date()): Promise<FileSummary[]> {
    return locked(() => {
      const state = load();
      return Object.values(state.files)
        .filter((file) => file.ownerId === ownerId && Date.parse(file.expiresAt) > now.getTime())
        .sort((a, b) => b.createdAt.localeCompare(a.createdAt))
        .map((file) => summary(state, file));
    });
  }

  getFile(ownerId: string, fileId: string): Promise<FileSummary> {
    return locked(() => {
      const state = load();
      const file = liveFile(state, ownerId, fileId);
      touch(state, file, new Date());
      save(state);
      return summary(state, file);
    });
  }

  readOwnedBlob(ownerId: string, fileId: string, hash: string): Promise<{ bytes: Buffer; file: FileRow }> {
    return locked(() => {
      const state = load();
      const file = liveFile(state, ownerId, fileId);
      const version = state.versions.some((item) => item.fileId === file.id && item.sha256 === hash);
      const proposal = Object.values(state.proposals).some(
        (item) => item.ownerId === ownerId && item.fileId === file.id && item.resultSha === hash && Date.parse(item.expiresAt) > Date.now()
      );
      if (!version && !proposal) throw new ToolError("That version is not part of this file.", "not_found");
      const now = Date.now();
      return { bytes: cachedBytes(ownerId, file.id, hash, file.expiresAt, now), file };
    });
  }

  readBytes(ownerId: string, fileId: string, hash?: string): Promise<{ bytes: Buffer; file: FileRow }> {
    return locked(() => {
      const state = load();
      const now = new Date();
      const file = liveFile(state, ownerId, fileId, now);
      const sha = hash ?? file.headSha;
      const known = state.versions.some((version) => version.fileId === file.id && version.sha256 === sha);
      if (!known) throw new ToolError("That version is not part of this file.", "not_found");
      touch(state, file, now);
      save(state);
      return { bytes: cachedBytes(ownerId, file.id, sha, file.expiresAt, now.getTime()), file };
    });
  }

  listVersions(ownerId: string, fileId: string) {
    return locked(() => {
      const state = load();
      const file = liveFile(state, ownerId, fileId);
      touch(state, file, new Date());
      save(state);
      return state.versions
        .filter((version) => version.fileId === file.id)
        .sort((a, b) => a.number - b.number)
        .map((version) => ({
          version: version.number,
          sha256: version.sha256,
          size: version.size,
          op: version.op,
          created_at: version.createdAt
        }));
    });
  }

  saveProposal(input: {
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
    return locked(() => {
      if (input.stored) {
        throw new ToolError("Stored proposals require the Supabase store.", "bad_config");
      }
      const state = load();
      const file = liveFile(state, input.ownerId, input.fileId);
      if (file.headSha !== input.baseSha) {
        throw new ToolError("base_version does not match the current file. Propose the edit again.", "stale_version");
      }
      const profile = requireEntitled(state, input.ownerId);
      const now = new Date();
      bump(state, input.ownerId, "proposals", tierFor(profile), now, usageUnits(file.size));
      writeBlob(input.resultSha, input.bytes);
      touch(state, file, now);
      headCachePut(headKey(input.ownerId, file.id, input.resultSha), input.bytes, file.expiresAt);
      const row: ProposalRow = {
        id: crypto.randomUUID(),
        ownerId: input.ownerId,
        fileId: file.id,
        baseSha: input.baseSha,
        resultSha: input.resultSha,
        size: input.size,
        diff: input.diff,
        warnings: input.warnings,
        verification: input.verification,
        createdAt: now.toISOString(),
        expiresAt: new Date(now.getTime() + 30 * 60 * 1000).toISOString()
      };
      state.proposals[row.id] = row;
      save(state);
      return row;
    });
  }

  getProposal(ownerId: string, proposalId: string): Promise<ProposalRow> {
    return locked(() => {
      const row = load().proposals[proposalId];
      if (!row || row.ownerId !== ownerId) throw new ToolError("Proposal not found.", "not_found");
      return row;
    });
  }

  publishEdit(ownerId: string, proposalId: string, baseVersion: string, now = new Date()): Promise<PublishResult> {
    return locked(() => {
      const state = load();
      const profile = requireEntitled(state, ownerId);
      const proposal = state.proposals[proposalId];
      if (!proposal || proposal.ownerId !== ownerId) throw new ToolError("Proposal not found.", "not_found");
      const file = liveFile(state, ownerId, proposal.fileId, now);
      if (proposal.baseSha !== baseVersion) {
        throw new ToolError("base_version does not match this proposal. Pass the token propose_edit returned.", "stale_version");
      }
      if (proposal.appliedAt && file.headSha === proposal.resultSha) {
        return {
          file_id: file.id,
          version: headVersion(state, file),
          sha256: file.headSha,
          size: file.size,
          idempotent: true,
          usage: usageView(tierFor(profile), countsOf(state, ownerId, now), profile.retention_hours, now)
        };
      }
      if (Date.parse(proposal.expiresAt) <= now.getTime()) {
        throw new ToolError("This proposal expired. Propose the edit again.", "expired");
      }
      if (file.headSha !== proposal.baseSha) {
        throw new ToolError("base_version does not match the current file. List versions and propose the edit again.", "stale_version");
      }
      const tier = tierFor(profile);
      bump(state, ownerId, "edits", tier, now, usageUnits(proposal.size));
      const number = headVersion(state, file) + 1;
      state.versions.push({
        fileId: file.id,
        number,
        sha256: proposal.resultSha,
        size: proposal.size,
        op: "apply",
        createdAt: now.toISOString()
      });
      file.headSha = proposal.resultSha;
      file.size = proposal.size;
      proposal.appliedAt = now.toISOString();
      touch(state, file, now);
      save(state);
      appendAudit({ ownerId, op: "apply_edit", fileId: file.id, sha256: proposal.resultSha, size: proposal.size, code: "ok" });
      return {
        file_id: file.id,
        version: number,
        sha256: proposal.resultSha,
        size: proposal.size,
        idempotent: false,
        usage: usageView(tier, countsOf(state, ownerId, now), profile.retention_hours, now)
      };
    });
  }

  recordRecalculation(ownerId: string, now = new Date(), sizeBytes = 1) {
    return locked(() => {
      const state = load();
      const profile = requireEntitled(state, ownerId);
      const tier = tierFor(profile);
      bump(state, ownerId, "recalculations", tier, now, usageUnits(sizeBytes));
      save(state);
      return usageView(tier, countsOf(state, ownerId, now), profile.retention_hours, now);
    });
  }

  assertRecalcAllowed(ownerId: string, now = new Date(), sizeBytes = 1): Promise<void> {
    return locked(() => {
      const state = load();
      const profile = requireEntitled(state, ownerId);
      const tier = tierFor(profile);
      const units = usageUnits(sizeBytes);
      const used = countsOf(state, ownerId, now).recalculations;
      const cap = planCaps(tier).recalculations;
      if (used + units > cap) throw new QuotaError("recalculations", used, cap, tier, now, units);
    });
  }

  restore(ownerId: string, fileId: string, version: number, now = new Date()): Promise<PublishResult> {
    return locked(() => {
      const state = load();
      const profile = requireEntitled(state, ownerId);
      const file = liveFile(state, ownerId, fileId, now);
      const source = state.versions.find((item) => item.fileId === file.id && item.number === version);
      if (!source) throw new ToolError("That version does not exist.", "not_found");
      const number = headVersion(state, file) + 1;
      state.versions.push({
        fileId: file.id,
        number,
        sha256: source.sha256,
        size: source.size,
        op: "restore",
        createdAt: now.toISOString()
      });
      file.headSha = source.sha256;
      file.size = source.size;
      touch(state, file, now);
      save(state);
      appendAudit({ ownerId, op: "restore_version", fileId: file.id, sha256: source.sha256, size: source.size, code: "ok" });
      return {
        file_id: file.id,
        version: number,
        sha256: source.sha256,
        size: source.size,
        idempotent: false,
        usage: usageView(tierFor(profile), countsOf(state, ownerId, now), profile.retention_hours, now)
      };
    });
  }

  deleteFile(ownerId: string, fileId: string): Promise<void> {
    return locked(() => {
      const state = load();
      const file = state.files[fileId];
      if (!file || file.ownerId !== ownerId) throw new ToolError("File not found.", "not_found");
      headCacheDropFile(ownerId, fileId);
      delete state.files[fileId];
      state.versions = state.versions.filter((version) => version.fileId !== fileId);
      for (const [id, proposal] of Object.entries(state.proposals)) {
        if (proposal.fileId === fileId) delete state.proposals[id];
      }
      save(state);
      appendAudit({ ownerId, op: "delete_file", fileId, code: "ok" });
    });
  }

  deleteAll(ownerId: string): Promise<number> {
    return locked(() => {
      const state = load();
      const ids = Object.values(state.files)
        .filter((file) => file.ownerId === ownerId)
        .map((file) => file.id);
      for (const id of ids) delete state.files[id];
      headCacheDropOwner(ownerId);
      state.versions = state.versions.filter((version) => !ids.includes(version.fileId));
      for (const [id, proposal] of Object.entries(state.proposals)) {
        if (proposal.ownerId === ownerId) delete state.proposals[id];
      }
      save(state);
      appendAudit({ ownerId, op: "delete_all", code: "ok" });
      return ids.length;
    });
  }

  purge(now = new Date()): Promise<{ files: number; blobs: number }> {
    return locked(() => {
      const state = load();
      const expired = Object.values(state.files).filter((file) => Date.parse(file.expiresAt) <= now.getTime());
      const ids = new Set(expired.map((file) => file.id));
      for (const file of expired) headCacheDropFile(file.ownerId, file.id);
      for (const id of ids) delete state.files[id];
      state.versions = state.versions.filter((version) => !ids.has(version.fileId));
      for (const [id, proposal] of Object.entries(state.proposals)) {
        if (ids.has(proposal.fileId) || Date.parse(proposal.expiresAt) <= now.getTime()) delete state.proposals[id];
      }
      for (const [nonce, upload] of Object.entries(state.uploads)) {
        if (upload.exp <= Math.floor(now.getTime() / 1000)) delete state.uploads[nonce];
      }
      const keep = referenced(state, now);
      let blobs = 0;
      const dir = path.join(dataDir(), "blobs");
      if (fs.existsSync(dir)) {
        for (const name of fs.readdirSync(dir)) {
          if (!keep.has(name)) {
            fs.rmSync(path.join(dir, name), { force: true });
            blobs += 1;
          }
        }
      }
      save(state);
      return { files: ids.size, blobs };
    });
  }

  audit(event: { ownerId: string; op: string; fileId?: string; sha256?: string; size?: number; code: string }): Promise<void> {
    return locked(() => {
      appendAudit(event);
    });
  }

  support(email: string, message: string): Promise<{ id: string }> {
    return locked(() => {
      const id = crypto.randomUUID();
      fs.mkdirSync(dataDir(), { recursive: true });
      fs.appendFileSync(
        path.join(dataDir(), "support.jsonl"),
        `${JSON.stringify({ id, email, message, at: new Date().toISOString() })}\n`
      );
      appendAudit({ ownerId: "support", op: "support", code: "ok" });
      return { id };
    });
  }

  /** Test helper: move a file's expiry into the past without changing usage. */
  expireFile(fileId: string, at: string): Promise<void> {
    return locked(() => {
      const state = load();
      const file = state.files[fileId];
      if (!file) throw new ToolError("File not found.", "not_found");
      file.expiresAt = at;
      headCacheDropFile(file.ownerId, fileId);
      save(state);
    });
  }
}

function requireEntitled(state: State, ownerId: string): Profile {
  const profile = state.profiles[ownerId];
  if (!profile || !entitled(profile)) {
    throw new ToolError("An active trial or Pro subscription is required.", "access");
  }
  return profile;
}

export function getStore(): Store {
  if (!cached) {
    if (useSupabaseStore()) {
      cached = new SupabaseStore();
    } else {
      cached = new LocalStore();
    }
  }
  return cached;
}

export function resetStoreForTests(): void {
  cached = undefined;
  chain = Promise.resolve();
  resetHeadCache();
  fs.rmSync(dataDir(), { recursive: true, force: true });
}
