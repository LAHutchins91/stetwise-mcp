import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import type { Server } from "node:http";
import { once } from "node:events";
import path from "node:path";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { repoRoot } from "../src/config.js";
import { engineAuthHeaders, engineCall } from "../src/engine-client.js";
import { assembleEnginePayload, assertHostedStore } from "../src/engine-files.js";
import { app } from "../src/server.js";

describe("hosting", () => {
  let server: Server;
  let base = "";

  beforeAll(async () => {
    server = app.listen(0);
    await once(server, "listening");
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    base = `http://127.0.0.1:${port}`;
  });

  afterAll(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  afterEach(() => {
    delete process.env.ENGINE_URL;
    delete process.env.ENGINE_HMAC_SECRET;
    delete process.env.CRON_SECRET;
  });

  it("uses functions and rewrites with a 300 second limit", () => {
    const config = JSON.parse(fs.readFileSync(path.join(repoRoot(), "vercel.json"), "utf8")) as {
      functions: Record<string, { maxDuration: number }>;
      rewrites: { source: string; destination: string }[];
      crons: { path: string; schedule: string }[];
      builds?: unknown;
      routes?: unknown;
    };
    expect(config.builds).toBeUndefined();
    expect(config.routes).toBeUndefined();
    expect(config.functions["src/server.ts"].maxDuration).toBe(300);
    expect(config.rewrites[0]).toEqual({ source: "/(.*)", destination: "/src/server.ts" });
    expect(config.crons[0]).toEqual({ path: "/internal/purge", schedule: "0 * * * *" });
  });

  it("authorizes GET purge with the cron bearer and keeps POST", async () => {
    const missing = await fetch(`${base}/internal/purge`);
    expect(missing.status).toBe(401);
    process.env.CRON_SECRET = "cron-test-secret";
    const wrong = await fetch(`${base}/internal/purge`, { headers: { authorization: "Bearer nope" } });
    expect(wrong.status).toBe(401);
    const ok = await fetch(`${base}/internal/purge`, { headers: { authorization: "Bearer cron-test-secret" } });
    expect(ok.status).toBe(200);
    const body = (await ok.json()) as { files: number; blobs: number };
    expect(typeof body.files).toBe("number");
    const posted = await fetch(`${base}/internal/purge`, {
      method: "POST",
      headers: { "x-stetwise-purge": "test-purge-secret" }
    });
    expect(posted.status).toBe(200);
  });

  it("does not offer Google sign-in", async () => {
    const consent = await fetch(`${base}/oauth/consent`);
    const consentHtml = await consent.text();
    expect(consentHtml).not.toContain("Continue with Google");
    expect(consentHtml).not.toContain("signInWithOAuth");
    expect(consentHtml).toContain("signInWithPassword");
    const home = await fetch(`${base}/`);
    const homeHtml = await home.text();
    expect(homeHtml).not.toContain("signInWithOAuth");
    expect(homeHtml).not.toContain("provider:'google'");
    expect(homeHtml).toContain("/oauth/consent");
  });

  it("signs engine calls with a timestamp and nonce and does not require file bytes", async () => {
    const heard: { headers: http.IncomingHttpHeaders; body: string }[] = [];
    const engine = http.createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        const body = Buffer.concat(chunks);
        heard.push({ headers: req.headers, body: body.toString("utf8") });
        const timestamp = String(req.headers["x-stetwise-timestamp"] || "");
        const nonce = String(req.headers["x-stetwise-nonce"] || "");
        const given = String(req.headers["x-stetwise-signature"] || "");
        const expected = crypto.createHmac("sha256", "engine-secret").update(`${timestamp}.${nonce}.`).update(body).digest("hex");
        if (given !== expected) {
          res.writeHead(401).end(JSON.stringify({ ok: false, error: { code: "unauthorized", reason: "bad signature" } }));
          return;
        }
        res.writeHead(200).end(JSON.stringify({ ok: true, result: { kind: "xlsx", sha256: "a".repeat(64), size: 12, stored: true } }));
      });
    });
    engine.listen(0, "127.0.0.1");
    await once(engine, "listening");
    const address = engine.address();
    const port = typeof address === "object" && address ? address.port : 0;
    process.env.ENGINE_URL = `http://127.0.0.1:${port}`;
    process.env.ENGINE_HMAC_SECRET = "engine-secret";
    try {
      const payload = assembleEnginePayload({
        hosted: true,
        action: "inspect",
        filename: "book.xlsx",
        sourceUrl: "https://storage.example/object",
        sourceKey: "owner/file",
        destUrl: "https://storage.example/upload",
        destKey: "owner/stage/1"
      });
      expect(payload).not.toHaveProperty("data_b64");
      expect(payload.source_url).toBe("https://storage.example/object");
      expect(payload.dest_key).toBe("owner/stage/1");
      const local = assembleEnginePayload({
        hosted: false,
        action: "inspect",
        filename: "book.xlsx",
        bytes: Buffer.from("hello")
      });
      expect(local.data_b64).toBe(Buffer.from("hello").toString("base64"));
      expect(local).not.toHaveProperty("source_url");
      const result = await engineCall<{ stored: boolean }>(payload);
      expect(result.stored).toBe(true);
      const headers = engineAuthHeaders("engine-secret", Buffer.from(heard[0].body));
      expect(headers["x-stetwise-timestamp"]).toMatch(/^\d+$/);
      expect(headers["x-stetwise-nonce"]).toMatch(/^[a-f0-9]{32}$/);
      expect(heard[0].headers["x-stetwise-timestamp"]).toMatch(/^\d+$/);
      expect(String(heard[0].headers["x-stetwise-nonce"]).length).toBeGreaterThanOrEqual(16);
      expect(heard[0].body).not.toContain("data_b64");
    } finally {
      await new Promise<void>((resolve) => engine.close(() => resolve()));
    }
  });

  it("refuses a remote engine without the Supabase store", () => {
    process.env.ENGINE_URL = "https://engine.example";
    expect(() => assertHostedStore()).toThrow(/Supabase store/);
  });
});
