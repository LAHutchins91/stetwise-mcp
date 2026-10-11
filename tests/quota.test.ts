import { beforeEach, describe, expect, it } from "vitest";
import { DEV_USER_ID } from "../src/config.js";
import { diskReadCount } from "../src/head-cache.js";
import { monthKey, planCaps, QuotaError, resetsAt, USAGE_UNIT_BYTES, usageUnits } from "../src/quota.js";
import { getStore, resetStoreForTests, sha256 } from "../src/store.js";

describe("monthly caps", () => {
  beforeEach(async () => {
    resetStoreForTests();
    delete process.env.STETWISE_CAP_TRIAL_EDITS;
    delete process.env.STETWISE_CAP_TRIAL_FILES;
    delete process.env.STETWISE_CAP_TRIAL_RECALCS;
    delete process.env.STETWISE_CAP_TRIAL_PROPOSALS;
    delete process.env.STETWISE_CAP_PRO_EDITS;
    delete process.env.STETWISE_CAP_PRO_RECALCS;
    delete process.env.STETWISE_CAP_PRO_PROPOSALS;
    await getStore().ensureDevProfile();
  });

  it("uses the product defaults and reads overrides from the environment", () => {
    expect(planCaps("pro")).toMatchObject({
      edits: 1000,
      files: 250,
      recalculations: 100,
      proposals: 2000,
      maxFileBytes: 25 * 1024 * 1024
    });
    expect(planCaps("trial")).toMatchObject({
      edits: 100,
      files: 25,
      recalculations: 20,
      proposals: 200,
      maxFileBytes: 25 * 1024 * 1024
    });
    expect(usageUnits(USAGE_UNIT_BYTES)).toBe(1);
    expect(usageUnits(USAGE_UNIT_BYTES + 1)).toBe(2);
    expect(usageUnits(25 * 1024 * 1024)).toBe(5);
    process.env.STETWISE_CAP_TRIAL_EDITS = "3";
    expect(planCaps("trial").edits).toBe(3);
  });

  it("counts usage per UTC month and blocks without an overage charge", async () => {
    const store = getStore();
    const january = new Date("2026-01-15T00:00:00Z");
    const february = new Date("2026-02-01T00:00:00Z");
    await store.replaceUsage(DEV_USER_ID, { edits: 100, recalculations: 20, files: 25 }, january);
    const inJanuary = await store.usage(DEV_USER_ID, january);
    const inFebruary = await store.usage(DEV_USER_ID, february);
    expect(inJanuary.edits.remaining).toBe(0);
    expect(inJanuary.overage_billing).toBe(false);
    expect(inFebruary.edits.used).toBe(0);
    expect(inFebruary.edits.remaining).toBe(100);
    expect(monthKey(february)).toBe("2026-02");
    await expect(store.recordRecalculation(DEV_USER_ID, january)).rejects.toBeInstanceOf(QuotaError);
    const blocked = await store.recordRecalculation(DEV_USER_ID, january).catch((error: unknown) => error);
    expect(blocked).toBeInstanceOf(QuotaError);
    expect((blocked as QuotaError).message).toContain("does not bill overage");
    expect((blocked as QuotaError).message).not.toMatch(/\$\d/);
    expect(resetsAt(january)).toBe("2026-02-01T00:00:00.000Z");
    const after = await store.usage(DEV_USER_ID, january);
    expect(after.recalculations.used).toBe(20);
  });

  it("keeps a trialing subscription on the trial edit and recalc caps", async () => {
    const store = getStore();
    await store.updateProfile(DEV_USER_ID, { plan: "pro", subscription_status: "trialing" });
    const view = await store.usage(DEV_USER_ID);
    expect(view.tier).toBe("trial");
    expect(view.edits.cap).toBe(100);
    expect(view.recalculations.cap).toBe(20);
    expect(view.proposals.cap).toBe(200);
    expect(view.overage_billing).toBe(false);
  });

  it("gives an active subscription the Pro caps", async () => {
    const store = getStore();
    await store.updateProfile(DEV_USER_ID, { plan: "annual", subscription_status: "active" });
    const view = await store.usage(DEV_USER_ID);
    expect(view.tier).toBe("pro");
    expect(view.edits.cap).toBe(1000);
    expect(view.files.cap).toBe(250);
    expect(view.recalculations.cap).toBe(100);
    expect(view.proposals.cap).toBe(2000);
    expect(view.max_file_bytes).toBe(25 * 1024 * 1024);
  });

  it("weights a file by each started 5 MB and does not partially count a blocked call", async () => {
    const store = getStore();
    await store.replaceUsage(DEV_USER_ID, { recalculations: 18, files: 24 });
    await expect(store.recordRecalculation(DEV_USER_ID, new Date(), 25 * 1024 * 1024)).rejects.toBeInstanceOf(QuotaError);
    expect((await store.usage(DEV_USER_ID)).recalculations.used).toBe(18);
    await store.recordRecalculation(DEV_USER_ID, new Date(), USAGE_UNIT_BYTES);
    expect((await store.usage(DEV_USER_ID)).recalculations.used).toBe(19);
    const big = Buffer.alloc(USAGE_UNIT_BYTES + 1, 1);
    await expect(
      store.commitNewFile({ ownerId: DEV_USER_ID, filename: "wide.xlsx", kind: "xlsx", bytes: big })
    ).rejects.toBeInstanceOf(QuotaError);
    expect((await store.usage(DEV_USER_ID)).files.used).toBe(24);
  });

  it("counts a saved proposal and blocks the next one at the cap", async () => {
    const store = getStore();
    process.env.STETWISE_CAP_TRIAL_PROPOSALS = "1";
    const saved = await store.commitNewFile({
      ownerId: DEV_USER_ID,
      filename: "note.xlsx",
      kind: "xlsx",
      bytes: Buffer.from("abc")
    });
    const bytes = Buffer.from("next");
    const input = {
      ownerId: DEV_USER_ID,
      fileId: saved.file_id,
      baseSha: saved.sha256,
      resultSha: sha256(bytes),
      size: bytes.length,
      bytes,
      diff: [],
      warnings: [],
      verification: {}
    };
    await store.saveProposal(input);
    expect((await store.usage(DEV_USER_ID)).proposals.used).toBe(1);
    await expect(store.saveProposal(input)).rejects.toBeInstanceOf(QuotaError);
    expect((await store.usage(DEV_USER_ID)).proposals.used).toBe(1);
    delete process.env.STETWISE_CAP_TRIAL_PROPOSALS;
  });

  it("reads a cached head once and drops it after retention", async () => {
    const store = getStore();
    const saved = await store.commitNewFile({
      ownerId: DEV_USER_ID,
      filename: "note.xlsx",
      kind: "xlsx",
      bytes: Buffer.from("cached-head")
    });
    const before = diskReadCount();
    const first = await store.readBytes(DEV_USER_ID, saved.file_id);
    const second = await store.readBytes(DEV_USER_ID, saved.file_id);
    expect(second.bytes.equals(first.bytes)).toBe(true);
    expect(diskReadCount()).toBe(before + 1);
    await store.expireFile(saved.file_id, "2000-01-01T00:00:00.000Z");
    await expect(store.readBytes(DEV_USER_ID, saved.file_id)).rejects.toThrow(/retention/);
  });
});
