import { intEnv } from "./config.js";
import { ToolError } from "./errors.js";

/** 25 MB as 25 × 1024 × 1024 bytes. */
export const DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024;

export const RETENTION_DEFAULT_HOURS = 24;
export const RETENTION_EXTENDED_HOURS = 168;

export type PlanTier = "pro" | "trial";
export type UsageMetric = "edits" | "files" | "recalculations";

export type PlanCaps = {
  edits: number;
  files: number;
  recalculations: number;
  maxFileBytes: number;
};

export type UsageCounts = Record<UsageMetric, number>;

/**
 * Caps are read on each call so operators can change them without a code edit.
 * Trial file cap defaults to 25 new files per UTC month. Pro is 250.
 * Neither tier is billed for usage past the cap; the call is refused.
 */
export function planCaps(tier: PlanTier): PlanCaps {
  if (tier === "pro") {
    return {
      edits: intEnv("STETWISE_CAP_PRO_EDITS", 1000),
      files: intEnv("STETWISE_CAP_PRO_FILES", 250),
      recalculations: intEnv("STETWISE_CAP_PRO_RECALCS", 200),
      maxFileBytes: intEnv("STETWISE_CAP_PRO_MAX_BYTES", DEFAULT_MAX_FILE_BYTES)
    };
  }
  return {
    edits: intEnv("STETWISE_CAP_TRIAL_EDITS", 100),
    files: intEnv("STETWISE_CAP_TRIAL_FILES", 25),
    recalculations: intEnv("STETWISE_CAP_TRIAL_RECALCS", 20),
    maxFileBytes: intEnv("STETWISE_CAP_TRIAL_MAX_BYTES", DEFAULT_MAX_FILE_BYTES)
  };
}

export function monthKey(now = new Date()): string {
  const month = String(now.getUTCMonth() + 1).padStart(2, "0");
  return `${now.getUTCFullYear()}-${month}`;
}

export function resetsAt(now = new Date()): string {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)).toISOString();
}

const METRIC_LABEL: Record<UsageMetric, string> = {
  edits: "applied edits",
  files: "files",
  recalculations: "recalculations"
};

export class QuotaError extends ToolError {
  constructor(
    readonly metric: UsageMetric,
    readonly used: number,
    readonly cap: number,
    readonly tier: PlanTier,
    now = new Date()
  ) {
    super(
      `Monthly ${METRIC_LABEL[metric]} cap reached (${used} of ${cap} on the ${tier} plan). This request was blocked. Stetwise does not bill overage. Usage resets ${resetsAt(now)}.`,
      "usage_cap"
    );
    this.name = "QuotaError";
  }
}

export class FileTooLargeError extends ToolError {
  constructor(size: number, maxBytes: number, tier: PlanTier) {
    super(
      `This file is ${size} bytes, above the ${tier} plan limit of ${maxBytes} bytes. Stetwise does not bill overage.`,
      "file_too_large"
    );
    this.name = "FileTooLargeError";
  }
}

export function tierFor(profile: { subscription_status: string; plan: string }): PlanTier {
  if (profile.subscription_status === "trialing") return "trial";
  if (profile.subscription_status === "active") return "pro";
  return "trial";
}

export function entitled(profile: { subscription_status: string }): boolean {
  return profile.subscription_status === "active" || profile.subscription_status === "trialing";
}

export function normalizeRetention(hours: number): 24 | 168 {
  if (hours === RETENTION_DEFAULT_HOURS || hours === RETENTION_EXTENDED_HOURS) return hours;
  throw new ToolError("Retention must be 24 hours or 168 hours (7 days).", "bad_retention");
}

export function expiryFrom(lastUsedAt: string, hours: number): string {
  return new Date(Date.parse(lastUsedAt) + hours * 60 * 60 * 1000).toISOString();
}

export function emptyUsage(): UsageCounts {
  return { edits: 0, files: 0, recalculations: 0 };
}

export function usageView(tier: PlanTier, counts: UsageCounts, retentionHours: number, now = new Date()) {
  const caps = planCaps(tier);
  const metric = (name: UsageMetric) => ({
    used: counts[name],
    cap: caps[name],
    remaining: Math.max(0, caps[name] - counts[name])
  });
  return {
    tier,
    month: monthKey(now),
    retention_hours: retentionHours,
    max_file_bytes: caps.maxFileBytes,
    edits: metric("edits"),
    files: metric("files"),
    recalculations: metric("recalculations"),
    resets_at: resetsAt(now),
    overage_billing: false
  };
}
