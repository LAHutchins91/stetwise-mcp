import crypto from "node:crypto";
import path from "node:path";
import { pathToFileURL } from "node:url";
import express, { type Request, type Response } from "express";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { billingPatch, checkoutForm, stripeEvent } from "./billing.js";
import {
  acceptUploads,
  appBaseUrl,
  DEV_USER_ID,
  devAuthEnabled,
  devToken,
  purgeSecret,
  priceIdForPlan,
  stripePriceMonthly,
  stripePriceYearly,
  stripeProductId,
  stripeSecret,
  supabaseAnonKey,
  useSupabaseStore,
  supabaseServiceKey,
  supabaseUrl
} from "./config.js";
import { engineCall } from "./engine-client.js";
import { ToolError } from "./errors.js";
import { logEvent } from "./log.js";
import { validateMcpClaims } from "./mcp-claims.js";
import { MCP_CORS_HEADERS, mcpBrowserOriginAllowed } from "./mcp-clients.js";
import { installPluginAuth } from "./plugin-auth.js";
import { installPublicPages, page } from "./public-pages.js";
import { entitled, FileTooLargeError, planCaps, tierFor, type PlanTier } from "./quota.js";
import { createStetwiseServer } from "./stetwise-tools.js";
import { getStore } from "./store.js";
import { assertFresh, readToken } from "./tokens.js";
import { landingConnectLead } from "./connect-page.js";

export const app = express();
export default app;
app.disable("x-powered-by");
app.set("trust proxy", 1);
app.use((_req, res, next) => {
  res.set({ "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "Cache-Control": "no-store" });
  next();
});

const publicLimits = new Map<string, { count: number; end: number }>();
function allowPublic(key: string, limit: number, ms: number) {
  const now = Date.now();
  for (const [name, value] of publicLimits) if (value.end <= now) publicLimits.delete(name);
  const value = publicLimits.get(key);
  if (value) {
    value.count += 1;
    return value.count <= limit;
  }
  if (publicLimits.size >= 10000) return false;
  publicLimits.set(key, { count: 1, end: now + ms });
  return true;
}

function bearer(req: Request) {
  const header = req.header("authorization") ?? "";
  return header.startsWith("Bearer ") ? header.slice(7).trim() : "";
}

async function supabaseRequest<T>(pathName: string, options: RequestInit = {}, accessToken?: string, serviceRole = false): Promise<T> {
  const base = supabaseUrl();
  const key = serviceRole ? supabaseServiceKey() : supabaseAnonKey();
  if (!base || !key) throw new ToolError("Sign-in is not configured.", "unconfigured");
  const headers = new Headers(options.headers);
  headers.set("apikey", key);
  headers.set("Authorization", `Bearer ${accessToken ?? key}`);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`${base}${pathName}`, { ...options, headers, signal: options.signal ?? AbortSignal.timeout(20000) });
  const text = await response.text();
  if (!response.ok) throw new ToolError("Sign-in could not be verified.", "unauthorized");
  return (text ? JSON.parse(text) : null) as T;
}

async function stripeRequest<T>(stripePath: string, params: URLSearchParams): Promise<T> {
  const secret = stripeSecret();
  if (!secret) throw new ToolError("Billing is not configured.", "billing_unconfigured");
  const response = await fetch(`https://api.stripe.com/v1/${stripePath}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${secret}`, "Content-Type": "application/x-www-form-urlencoded" },
    body: params.toString()
  });
  const body = await response.text();
  if (!response.ok) throw new ToolError("Billing could not be reached.", "billing");
  return JSON.parse(body) as T;
}

type AuthUser = { id: string; email?: string };

async function authenticatedUser(req: Request): Promise<AuthUser> {
  const token = bearer(req);
  if (!token) throw new ToolError("Sign in to Stetwise.", "unauthorized");
  if (devAuthEnabled() && token === devToken()) {
    await getStore().ensureDevProfile();
    return { id: DEV_USER_ID, email: "dev@stetwise.local" };
  }
  const user = await supabaseRequest<AuthUser>("/auth/v1/user", { method: "GET" }, token);
  if (!user?.id) throw new ToolError("Sign in to Stetwise.", "unauthorized");
  validateMcpClaims(token, user.id, `${supabaseUrl()}/auth/v1`, `${appBaseUrl()}/mcp`);
  await getStore().ensureProfile({ id: user.id, email: user.email });
  return user;
}

app.post("/billing/webhook", express.raw({ type: "application/json" }), async (req, res) => {
  try {
    const signature = req.header("stripe-signature");
    if (!signature || !Buffer.isBuffer(req.body)) return res.status(400).send("Missing Stripe signature");
    const event = stripeEvent(req.body, signature);
    const update = billingPatch(event.type, event.data.object);
    if (update?.userId) {
      await getStore().ensureProfile({ id: update.userId });
      await getStore().updateProfile(update.userId, update.patch);
    }
    logEvent({ op: "stripe_webhook", code: "ok" });
    res.json({ received: true });
  } catch {
    logEvent({ op: "stripe_webhook", code: "rejected" });
    res.status(400).send("Webhook could not be processed");
  }
});

app.use(express.json({ limit: "128kb" }));
installPublicPages(app);
app.get("/.well-known/openai-apps-challenge", (_req, res) => {
  const token = process.env.OPENAI_APPS_CHALLENGE;
  if (!token) return res.status(404).type("text").send("Verification is not configured.");
  res.type("text").send(token);
});
app.post("/api/support", async (req, res) => {
  if (!allowPublic(`support:${req.ip}`, 5, 3_600_000)) return res.status(429).json({ error: "Too many requests. Please try again in an hour." });
  const input = z.object({ email: z.string().email().max(254), message: z.string().trim().min(10).max(4000) }).safeParse(req.body);
  if (!input.success) return res.status(400).json({ error: "Enter a valid reply email and a message of 10–4000 characters." });
  const saved = await getStore().support(input.data.email, input.data.message);
  logEvent({ op: "support", code: "ok" });
  res.json({ id: saved.id });
});
app.get("/data", (_req, res) => {
  res.type("html").send(
    page(
      "Your data",
      `<p>Files are deleted 24 hours after last use unless you set 7-day retention. You can delete every file now. This does not cancel billing.</p>
<p>Sign in on a host with Supabase configured, or call <code>delete_file</code> from a connected assistant. <a href="/support">Contact support</a> for account deletion.</p>
<button id="delete" type="button">Delete all of my files</button><p id="message" role="status"></p>
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.115.0/dist/umd/supabase.js"></script>
<script>
document.getElementById('delete').onclick=async function(){
  this.disabled=true;
  var message=document.getElementById('message');
  try{
    var cfg=${JSON.stringify({ url: supabaseUrl(), key: supabaseAnonKey() }).replace(/</g, "\\u003c")};
    if(!cfg.url||!window.supabase) throw Error('Sign-in is not configured on this host.');
    var client=window.supabase.createClient(cfg.url,cfg.key);
    var session=await client.auth.getSession();
    if(!session.data.session) throw Error('Sign in first.');
    var response=await fetch('/api/account/delete-files',{method:'POST',headers:{Authorization:'Bearer '+session.data.session.access_token}});
    var body=await response.json();
    if(!response.ok) throw Error(body.error&&body.error.reason||'Delete failed.');
    message.textContent='Deleted '+body.deleted+' file record(s).';
  }catch(error){message.textContent=error.message}finally{this.disabled=false}
};
</script>`
    )
  );
});
installPluginAuth(app, appBaseUrl(), supabaseUrl(), supabaseAnonKey());

app.get("/", (_req, res) => {
  const configured = Boolean(supabaseUrl() && supabaseAnonKey());
  res.type("html").send(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Stetwise by Ouroboros</title><link rel="icon" href="/icon.svg">
<style>body{margin:0;background:#0b1020;color:#edf1ff;font:17px/1.6 system-ui}main{max-width:880px;margin:0 auto;padding:32px 20px 64px}a{color:#b8adff}h1{font-size:48px;line-height:1.05;letter-spacing:-.04em}code{overflow-wrap:anywhere}.plans{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:700px){.plans{grid-template-columns:1fr}}article{border:1px solid #39445f;border-radius:16px;padding:18px}button{font:inherit;padding:12px 16px;border-radius:10px;border:0;background:#2945a6;color:white;cursor:pointer}</style></head>
<body><main><p>Stetwise by Ouroboros</p><h1>Office edits that leave everything else standing.</h1>
<p>Preview a change to an Excel or Word file, apply only that change, and undo it. Charts, pivots, styles, comments, and tracked changes stay unless you asked to touch them. If that cannot be guaranteed, the edit is refused.</p>
${landingConnectLead(appBaseUrl())}
<section class="plans" id="plans"><article><h2>Monthly</h2><p>14-day trial, then Pro. Stripe shows the amount before you pay.</p><button class="checkout" data-plan="monthly">Start monthly trial</button></article>
<article><h2>Yearly</h2><p>The same trial and the same caps, billed once a year. Stripe shows the amount before you pay.</p><button class="checkout" data-plan="annual">Start yearly trial</button></article></section>
<p id="message" role="status"></p>
<p>Trial: 100 applied edits, 25 new files, 200 proposals, and 20 recalculations per UTC month. Pro: 1,000 applied edits, 250 new files, 2,000 proposals, and 100 recalculations. Each started 5 MB of a file counts as one unit. Files stay at or under 25 MB. Past a cap, the call is blocked and nothing extra is billed. Files expire 24 hours after last use, or after 7 days if you choose that.</p>
<p><a href="/privacy">Privacy</a> · <a href="/terms">Terms</a> · <a href="/support">Support</a> · <a href="mailto:ouroborosplugins@gmail.com">ouroborosplugins@gmail.com</a></p>
<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.115.0/dist/umd/supabase.js"></script>
<script>
(function(){
var configured=${configured ? "true" : "false"};
var cfg=${JSON.stringify({ url: supabaseUrl(), key: supabaseAnonKey() }).replace(/</g, "\\u003c")};
document.querySelectorAll('.checkout').forEach(function(btn){btn.onclick=async function(){
  var message=document.getElementById('message');
  if(!configured||!window.supabase){message.textContent='Sign-in is configured when this host has a Supabase project.';return}
  btn.disabled=true;
  try{
    var client=window.supabase.createClient(cfg.url,cfg.key);
    var session=await client.auth.getSession();
    if(!session.data.session){await client.auth.signInWithOAuth({provider:'google',options:{redirectTo:location.origin+'/'}});return}
    var response=await fetch('/billing/checkout',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+session.data.session.access_token},body:JSON.stringify({plan:btn.getAttribute('data-plan')})});
    var data=await response.json();
    if(!response.ok) throw Error(data.error||'Checkout could not start.');
    location.href=data.url;
  }catch(error){message.textContent=error.message;btn.disabled=false}
}});
})();
</script></main></body></html>`);
});

app.get("/health", (_req, res) => {
  res.json({
    ok: true,
    service: "stetwise",
    brand: "Stetwise by Ouroboros",
    version: "0.1.0",
    supabaseConfigured: Boolean(supabaseUrl() && supabaseAnonKey()),
    stripeSecretConfigured: Boolean(stripeSecret()),
    stripeProductConfigured: Boolean(stripeProductId()),
    stripeMonthlyConfigured: Boolean(stripePriceMonthly()),
    stripeYearlyConfigured: Boolean(stripePriceYearly()),
    billingConfigured: Boolean(stripeSecret() && stripeProductId() && stripePriceMonthly() && stripePriceYearly()),
    engineUrlConfigured: Boolean(process.env.ENGINE_URL),
    store: useSupabaseStore() ? "supabase" : "local",
    acceptUploads: acceptUploads(),
    fileHostConfigured: Boolean(process.env.STETWISE_FILE_BASE_URL)
  });
});

app.post("/billing/checkout", async (req, res) => {
  try {
    const user = await authenticatedUser(req);
    const plan = req.body?.plan === "annual" ? "annual" : "monthly";
    const price = priceIdForPlan(plan);
    if (!price || !stripeProductId() || !stripeSecret()) return res.status(503).json({ error: "Billing is not configured." });
    const profile = await getStore().getProfile(user.id);
    const params = checkoutForm({
      userId: user.id,
      email: user.email,
      priceId: price,
      customerId: profile?.stripe_customer_id,
      baseUrl: appBaseUrl()
    });
    const session = await stripeRequest<{ id: string; url: string }>("checkout/sessions", params);
    res.json({ id: session.id, url: session.url });
  } catch {
    res.status(400).json({ error: "Unable to create checkout. Verify sign-in and retry." });
  }
});

app.post("/billing/portal", async (req, res) => {
  try {
    const user = await authenticatedUser(req);
    const profile = await getStore().getProfile(user.id);
    if (!profile?.stripe_customer_id) return res.status(400).json({ error: "No Stripe customer exists for this account yet." });
    const params = new URLSearchParams({ customer: profile.stripe_customer_id, return_url: appBaseUrl() });
    const session = await stripeRequest<{ url: string }>("billing_portal/sessions", params);
    res.json({ url: session.url });
  } catch {
    res.status(400).json({ error: "Unable to open billing management. Verify sign-in and retry." });
  }
});

app.post("/api/account/delete-files", async (req, res) => {
  try {
    const user = await authenticatedUser(req);
    const deleted = await getStore().deleteAll(user.id);
    logEvent({ op: "delete_all", code: "ok" });
    res.json({ deleted });
  } catch (error) {
    const reason = error instanceof ToolError ? error.message : "Delete failed.";
    res.status(401).json({ error: { code: "unauthorized", reason } });
  }
});

function readLimited(req: Request, max: number, tier: PlanTier): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    let settled = false;
    const fail = (error: Error) => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    req.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > max) {
        fail(new FileTooLargeError(size, max, tier));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (settled) return;
      settled = true;
      if (size === 0) reject(new ToolError("The upload was empty.", "empty_upload"));
      else resolve(Buffer.concat(chunks));
    });
    req.on("error", () => fail(new ToolError("Upload failed. The file was not stored.", "upload_failed")));
  });
}

function httpError(error: unknown, res: Response) {
  const code = error instanceof ToolError ? error.code : "request_failed";
  const reason = error instanceof ToolError ? error.message : "The request failed. The file was not changed.";
  const status = code === "file_too_large" ? 413 : code === "usage_cap" ? 429 : code === "unauthorized" || code === "bad_link" || code === "expired_link" ? 401 : code === "not_found" || code === "expired_file" ? 404 : 400;
  logEvent({ op: "http", code });
  res.status(status).json({ error: { code, reason } });
}

function rejectFileBody(res: Response): void {
  res.status(421).json({
    error: {
      code: "use_file_host",
      reason: "This host does not accept file bodies. Use the upload or download link exactly as the tool returned it."
    }
  });
}

app.put("/v1/upload/:token", async (req, res) => {
  if (!acceptUploads()) {
    rejectFileBody(res);
    req.destroy();
    return;
  }
  try {
    const parsed = readToken(String(req.params.token));
    if (parsed.typ !== "put") throw new ToolError("This link is not an upload link.", "bad_link");
    assertFresh(parsed.exp);
    const profile = await getStore().getProfile(parsed.ownerId);
    if (!profile || !entitled(profile)) throw new ToolError("An active trial or Pro subscription is required.", "access");
    const tier = tierFor(profile);
    const bytes = await readLimited(req, planCaps(tier).maxFileBytes, tier);
    const inspected = await engineCall<{ kind: string }>({
      action: "inspect",
      filename: parsed.filename,
      data_b64: bytes.toString("base64")
    });
    const saved = await getStore().commitNewFile({
      ownerId: parsed.ownerId,
      filename: parsed.filename,
      kind: inspected.kind,
      bytes,
      nonce: parsed.nonce
    });
    logEvent({ op: "upload", code: "ok", sha256: saved.sha256, size: saved.size });
    res.json(saved);
  } catch (error) {
    httpError(error, res);
  }
});

app.get("/v1/download/:token", async (req, res) => {
  if (!acceptUploads()) return rejectFileBody(res);
  try {
    const parsed = readToken(String(req.params.token));
    if (parsed.typ !== "get") throw new ToolError("This link is not a download link.", "bad_link");
    assertFresh(parsed.exp);
    const loaded = await getStore().readBytes(parsed.ownerId, parsed.fileId, parsed.sha);
    const filename = loaded.file.filename.replace(/["\r\n]/g, "");
    const type = filename.toLowerCase().endsWith(".docx")
      ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
      : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
    logEvent({ op: "download", code: "ok", sha256: parsed.sha, size: loaded.bytes.length });
    res.set({ "Content-Type": type, "Content-Length": String(loaded.bytes.length), "Content-Disposition": `attachment; filename="${filename}"` });
    res.end(loaded.bytes);
  } catch (error) {
    httpError(error, res);
  }
});

app.post("/internal/purge", async (req, res) => {
  const secret = purgeSecret();
  const given = req.header("x-stetwise-purge") || "";
  if (!secret || given.length !== secret.length || !timingSafe(given, secret)) {
    return res.status(401).json({ error: { code: "unauthorized", reason: "Purge is not authorized." } });
  }
  const result = await getStore().purge();
  logEvent({ op: "purge", code: "ok" });
  res.json(result);
});

function timingSafe(given: string, expected: string) {
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function guardMcpOrigin(req: Request, res: Response) {
  const origin = req.header("origin");
  if (!mcpBrowserOriginAllowed(origin, appBaseUrl())) {
    res.status(403).json({ error: "Origin is not allowed." });
    return false;
  }
  if (origin) res.set({ ...MCP_CORS_HEADERS, "Access-Control-Allow-Origin": origin, Vary: "Origin" });
  return true;
}

app.options("/mcp", (req, res) => {
  if (!guardMcpOrigin(req, res)) return;
  res.status(204).end();
});

app.post("/mcp", async (req, res) => {
  if (!guardMcpOrigin(req, res)) return;
  if (!allowPublic(`mcp:${req.ip}`, 300, 60_000)) return res.status(429).set("Retry-After", "60").json({ error: "Too many requests. Retry in one minute." });
  const publicMethods = new Set(["initialize", "notifications/initialized", "tools/list", "ping"]);
  let ownerId = "";
  if (!publicMethods.has(req.body?.method)) {
    try {
      const user = await authenticatedUser(req);
      ownerId = user.id;
    } catch {
      res.set("WWW-Authenticate", `Bearer resource_metadata="${appBaseUrl()}/.well-known/oauth-protected-resource/mcp"`);
      return res.status(401).json({ error: "Sign in to Stetwise to use these tools." });
    }
    const profile = await getStore().getProfile(ownerId);
    if (!profile || !entitled(profile)) {
      return res.status(403).json({
        error: "A Stetwise Pro subscription or active trial is required.",
        access_information: `${appBaseUrl()}/access`
      });
    }
  }
  let server: McpServer | undefined;
  try {
    server = createStetwiseServer(ownerId);
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
    res.on("close", () => {
      void transport.close();
      void server?.close();
    });
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch {
    if (!res.headersSent) res.status(500).json({ error: "Unable to process the plugin request." });
  }
});

app.get("/mcp", (req, res) => {
  if (!guardMcpOrigin(req, res)) return;
  res.set("WWW-Authenticate", `Bearer resource_metadata="${appBaseUrl()}/.well-known/oauth-protected-resource/mcp"`);
  res.status(401).json({ error: "Use Streamable HTTP POST with your Stetwise connection." });
});

app.use((error: { type?: string }, _req: Request, res: Response, _next: express.NextFunction) => {
  res.status(error?.type === "entity.too.large" ? 413 : 400).json({ error: "Invalid or oversized request." });
});

function runningAsEntry() {
  const entry = process.argv[1];
  if (!entry) return false;
  return import.meta.url === pathToFileURL(path.resolve(entry)).href;
}

if (process.env.NODE_ENV !== "test" && !process.env.VITEST && runningAsEntry()) {
  const port = Number(process.env.PORT ?? 3000);
  const stdio = process.stdin.isTTY !== true && process.env.STETWISE_HTTP_ONLY !== "1";
  app.listen(port, () => {
    const line = `Stetwise by Ouroboros listening on ${port}`;
    if (stdio) console.error(line);
    else console.log(line);
  });
  if (stdio) {
    const stdioServer = createStetwiseServer("");
    await stdioServer.connect(new StdioServerTransport());
  }
}
