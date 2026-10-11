import crypto from "node:crypto";
import { stripePriceYearly, stripeProductId, stripeWebhookSecret } from "./config.js";
import { ToolError } from "./errors.js";
import type { Profile } from "./store.js";

type JsonObject = Record<string, unknown>;

export function checkoutForm(input: {
  userId: string;
  email?: string;
  priceId: string;
  customerId?: string | null;
  baseUrl: string;
}): URLSearchParams {
  const params = new URLSearchParams();
  // Reference an existing Price on the Stetwise product. Do not define an inline amount or create a Price.
  params.set("mode", "subscription");
  params.set("line_items[0][price]", input.priceId);
  params.set("line_items[0][quantity]", "1");
  params.set("client_reference_id", input.userId);
  params.set("metadata[supabase_user_id]", input.userId);
  params.set("metadata[stripe_product_id]", stripeProductId());
  params.set("subscription_data[metadata][supabase_user_id]", input.userId);
  params.set("subscription_data[metadata][stripe_product_id]", stripeProductId());
  params.set("subscription_data[trial_period_days]", "14");
  params.set("payment_method_collection", "always");
  params.set("success_url", `${input.baseUrl}/?checkout=success`);
  params.set("cancel_url", `${input.baseUrl}/?checkout=cancelled`);
  if (input.customerId) params.set("customer", input.customerId);
  else if (input.email) params.set("customer_email", input.email);
  return params;
}

export function stripeEvent(rawBody: Buffer, signatureHeader: string): { type: string; data: { object: JsonObject } } {
  const secret = stripeWebhookSecret();
  if (!secret) throw new ToolError("Stripe webhooks are not configured.", "billing_unconfigured");
  const fields = signatureHeader.split(",").map((part) => part.trim());
  const timestamp = fields.find((part) => part.startsWith("t="))?.slice(2);
  const signatures = fields.filter((part) => part.startsWith("v1=")).map((part) => part.slice(3));
  if (!timestamp || !/^\d+$/.test(timestamp) || signatures.length === 0) throw new ToolError("Malformed Stripe signature.", "billing");
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > 300) throw new ToolError("Expired Stripe signature.", "billing");
  const expected = crypto.createHmac("sha256", secret).update(`${timestamp}.${rawBody.toString("utf8")}`).digest("hex");
  const valid = signatures.some((signature) => signature.length === expected.length && crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected)));
  if (!valid) throw new ToolError("Invalid Stripe signature.", "billing");
  return JSON.parse(rawBody.toString("utf8")) as { type: string; data: { object: JsonObject } };
}

/** Map a Stripe subscription event onto profile columns. Never creates an overage charge. */
export function billingPatch(eventType: string, object: JsonObject): { userId?: string; patch: Partial<Profile> } | null {
  if (eventType === "checkout.session.completed") {
    const userId = typeof object.client_reference_id === "string" ? object.client_reference_id : undefined;
    if (!userId) return null;
    return {
      userId,
      patch: {
        stripe_customer_id: typeof object.customer === "string" ? object.customer : null,
        stripe_subscription_id: typeof object.subscription === "string" ? object.subscription : null
      }
    };
  }
  if (!["customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"].includes(eventType)) {
    return null;
  }
  const metadata = (object.metadata ?? {}) as JsonObject;
  const userId = typeof metadata.supabase_user_id === "string" ? metadata.supabase_user_id : undefined;
  if (!userId) return null;
  const status = typeof object.status === "string" ? object.status : "unknown";
  const periodEnd = typeof object.current_period_end === "number" ? new Date(object.current_period_end * 1000).toISOString() : null;
  const priceId = (((object.items as JsonObject | undefined)?.data as JsonObject[] | undefined)?.[0]?.price as JsonObject | undefined)?.id;
  const yearly = stripePriceYearly();
  const plan = priceId && yearly && priceId === yearly ? "annual" : ["active", "trialing"].includes(status) ? "pro" : "trial";
  return {
    userId,
    patch: {
      plan,
      stripe_customer_id: typeof object.customer === "string" ? object.customer : null,
      stripe_subscription_id: typeof object.id === "string" ? object.id : null,
      subscription_status: status,
      current_period_end: periodEnd,
      cancel_at_period_end: Boolean(object.cancel_at_period_end)
    }
  };
}
