import fs from "node:fs";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { billingPatch, checkoutForm } from "../src/billing.js";
import { priceIdForPlan, repoRoot, stripePriceMonthly, stripePriceYearly, stripeProductId } from "../src/config.js";

describe("billing", () => {
  afterEach(() => {
    delete process.env.STRIPE_PRODUCT_ID;
    delete process.env.STRIPE_PRICE_MONTHLY;
    delete process.env.STRIPE_PRICE_YEARLY;
  });

  it("uses the live Stripe product and prices from config, overridable by env", () => {
    expect(stripeProductId()).toBe("prod_VQ12q90uKDcoto");
    expect(stripePriceMonthly()).toBe("price_1UPB0453QnCs3zydekVagtYa");
    expect(stripePriceYearly()).toBe("price_1UPB0953QnCs3zydvFaCDgDw");
    expect(priceIdForPlan(undefined)).toBe(stripePriceMonthly());
    expect(priceIdForPlan("monthly")).toBe(stripePriceMonthly());
    expect(priceIdForPlan("annual")).toBe(stripePriceYearly());
    process.env.STRIPE_PRICE_MONTHLY = "price_override_month";
    process.env.STRIPE_PRICE_YEARLY = "price_override_year";
    expect(priceIdForPlan("monthly")).toBe("price_override_month");
    expect(priceIdForPlan("annual")).toBe("price_override_year");
  });

  it("starts a 14-day trial on the existing monthly price and does not create a price", () => {
    const params = checkoutForm({
      userId: "00000000-0000-4000-8000-000000000001",
      email: "person@example.com",
      priceId: priceIdForPlan("monthly"),
      baseUrl: "http://127.0.0.1:9"
    });
    const body = params.toString();
    expect(params.get("subscription_data[trial_period_days]")).toBe("14");
    expect(params.get("payment_method_collection")).toBe("if_required");
    expect(params.get("subscription_data[trial_settings][end_behavior][missing_payment_method]")).toBe("cancel");
    expect(params.get("line_items[0][price]")).toBe("price_1UPB0453QnCs3zydekVagtYa");
    expect(params.get("metadata[stripe_product_id]")).toBe("prod_VQ12q90uKDcoto");
    expect(body).not.toContain("price_data");
    expect(body).not.toContain("product_data");
    expect(body).not.toContain("unit_amount");
    expect(body).not.toContain("/products");
    expect(body).not.toContain("/prices");
    expect(body).not.toMatch(/\$\d/);
    const source = ["src/billing.ts", "src/server.ts", "src/config.ts"]
      .map((file) => fs.readFileSync(path.join(repoRoot(), file), "utf8"))
      .join("\n");
    expect(source).not.toContain("price_data");
    expect(source).not.toContain("/v1/products");
    expect(source).not.toContain("/v1/prices");
  });

  it("maps a subscription onto plan status and does not invent a usage charge", () => {
    const update = billingPatch("customer.subscription.updated", {
      id: "sub_123",
      customer: "cus_123",
      status: "trialing",
      current_period_end: 1_800_000_000,
      cancel_at_period_end: false,
      metadata: { supabase_user_id: "user-1" },
      items: { data: [{ price: { id: "price_month" } }] }
    });
    expect(update?.userId).toBe("user-1");
    expect(update?.patch.subscription_status).toBe("trialing");
    expect(update?.patch.plan).toBe("pro");
    expect(JSON.stringify(update)).not.toMatch(/\$\d/);
    expect(JSON.stringify(update)).not.toContain("invoice");
  });
});
