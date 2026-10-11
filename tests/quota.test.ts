import { beforeEach, describe, expect, it } from "vitest";
import { DEV_USER_ID } from "../src/config.js";
import { monthKey, planCaps, QuotaError, resetsAt } from "../src/quota.js";
import { getStore, resetStoreForTests } from "../src/store.js";

describe("monthly caps", () => {
  beforeEach(async () => {
    resetStoreForTests();
    delete process.env.STETWISE_CAP_TRIAL_EDITS;
    delete process.env.STETWISE_CAP_TRIAL_FILES;
    delete process.env.STETWISE_CAP_TRIAL_RECALCS;
    delete process.env.STETWISE_CAP_PRO_EDITS;
    await getStore().ensureDevProfile();
  });

  it("uses the product defaults and reads overrides from the environment", () => {
    expect(planCaps("pro")).toMatchObject({ edits: 1000, files: 250, recalculations: 200, maxFileBytes: 25 * 1024 * 1024 });
    expect(planCaps("trial")).toMatchObject({ edits: 100, files: 25, recalculations: 20, maxFileBytes: 25 * 1024 * 1024 });
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
    expect(view.overage_billing).toBe(false);
  });

  it("gives an active subscription the Pro caps", async () => {
    const store = getStore();
    await store.updateProfile(DEV_USER_ID, { plan: "annual", subscription_status: "active" });
    const view = await store.usage(DEV_USER_ID);
    expect(view.tier).toBe("pro");
    expect(view.edits.cap).toBe(1000);
    expect(view.files.cap).toBe(250);
    expect(view.recalculations.cap).toBe(200);
    expect(view.max_file_bytes).toBe(25 * 1024 * 1024);
  });
});
