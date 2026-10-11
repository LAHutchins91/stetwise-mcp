import http from "node:http";
import { once } from "node:events";
import { afterEach, describe, expect, it } from "vitest";
import { QuotaError } from "../src/quota.js";
import { SupabaseStore } from "../src/supabase-store.js";
import { getStore, LocalStore, resetStoreForTests, sha256 } from "../src/store.js";
import fs from "node:fs";
import path from "node:path";
import { repoRoot } from "../src/config.js";

const USER = "00000000-0000-4000-8000-0000000000aa";

type Heard = { method: string; url: string; headers: http.IncomingHttpHeaders; body: Buffer };

function listen(handler: (req: http.IncomingMessage, res: http.ServerResponse, body: Buffer) => void) {
  const heard: Heard[] = [];
  const server = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const body = Buffer.concat(chunks);
      heard.push({ method: req.method || "", url: req.url || "", headers: req.headers, body });
      handler(req, res, body);
    });
  });
  return { server, heard };
}

describe("supabase store", () => {
  let server: http.Server | undefined;

  afterEach(async () => {
    delete process.env.SUPABASE_URL;
    delete process.env.SUPABASE_SERVICE_ROLE_KEY;
    delete process.env.STETWISE_STORE;
    if (server) {
      const closing = server;
      server = undefined;
      await new Promise<void>((resolve) => closing.close(() => resolve()));
    }
    resetStoreForTests();
  });

  it("keeps the local directory store for dev and tests", () => {
    process.env.SUPABASE_URL = "https://example.supabase.co";
    process.env.SUPABASE_SERVICE_ROLE_KEY = "service-role";
    process.env.STETWISE_STORE = "local";
    resetStoreForTests();
    expect(getStore()).toBeInstanceOf(LocalStore);
  });

  it("uploads blobs then commits through the schema functions, and maps a cap refusal", async () => {
    const blobs = new Map<string, Buffer>();
    const { server: httpServer, heard } = listen((req, res, body) => {
      const url = req.url || "";
      if (!req.headers.authorization?.startsWith("Bearer ") || !req.headers.apikey) {
        res.writeHead(401).end("{}");
        return;
      }
      if (req.method === "GET" && url.startsWith("/rest/v1/profiles")) {
        res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify([{
          id: USER,
          email: "person@example.com",
          plan: "pro",
          subscription_status: "trialing",
          retention_hours: 24,
          stripe_customer_id: null,
          stripe_subscription_id: null,
          current_period_end: null,
          cancel_at_period_end: false
        }]));
        return;
      }
      if (req.method === "POST" && url.startsWith("/storage/v1/object/stetwise-files/")) {
        blobs.set(url.slice("/storage/v1/object/stetwise-files/".length), body);
        res.writeHead(200).end("{}");
        return;
      }
      if (req.method === "POST" && url === "/rest/v1/rpc/stetwise_commit_file") {
        const args = JSON.parse(body.toString("utf8")) as { p_sha: string; p_files_cap: number; p_tier: string; p_max_bytes: number };
        if (args.p_files_cap === 1) {
          res.writeHead(400, { "content-type": "application/json" }).end(JSON.stringify({
            code: "P0001",
            message: "stetwise:usage_cap:files:1:1:trial"
          }));
          return;
        }
        expect(args.p_tier).toBe("trial");
        expect(args.p_max_bytes).toBe(25 * 1024 * 1024);
        expect(blobs.has(`${USER}/${args.p_sha}`)).toBe(true);
        res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({
          file_id: "00000000-0000-4000-8000-0000000000bb",
          filename: "note.docx",
          kind: "docx",
          size: body.length,
          sha256: args.p_sha,
          version: 1,
          created_at: "2026-10-11T00:00:00.000Z",
          last_used_at: "2026-10-11T00:00:00.000Z",
          expires_at: "2026-10-12T00:00:00.000Z"
        }));
        return;
      }
      if (req.method === "POST" && url === "/rest/v1/rpc/stetwise_purge") {
        res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({
          files: 1,
          drop: [{ owner_id: USER, sha256: "a".repeat(64) }],
          keep: []
        }));
        return;
      }
      if (req.method === "POST" && url === "/storage/v1/object/list/stetwise-files") {
        const args = JSON.parse(body.toString("utf8")) as { prefix: string };
        if (args.prefix === "") {
          res.writeHead(200).end(JSON.stringify([{ name: USER, id: null }]));
          return;
        }
        const fresh = new Date().toISOString();
        res.writeHead(200).end(JSON.stringify([
          { name: "b".repeat(64), id: "obj" },
          { name: `incoming/${"c".repeat(32)}`, id: "in", created_at: fresh },
          { name: "stage/00000000-0000-4000-8000-0000000000cc", id: "st", created_at: "2020-01-01T00:00:00.000Z" }
        ]));
        return;
      }
      if (req.method === "DELETE" && url === "/storage/v1/object/stetwise-files") {
        res.writeHead(200).end("[]");
        return;
      }
      res.writeHead(404).end("{}");
    });
    server = httpServer;
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    process.env.SUPABASE_URL = `http://127.0.0.1:${port}`;
    process.env.SUPABASE_SERVICE_ROLE_KEY = "service-role-test";
    process.env.STETWISE_CAP_TRIAL_FILES = "25";

    const store = new SupabaseStore();
    const bytes = Buffer.from("hello stetwise");
    const saved = await store.commitNewFile({ ownerId: USER, filename: "note.docx", kind: "docx", bytes });
    expect(saved.sha256).toBe(sha256(bytes));
    expect(saved.expires_at).toBe("2026-10-12T00:00:00.000Z");
    expect(heard.some((call) => call.method === "POST" && call.url.includes("/storage/v1/object/stetwise-files/"))).toBe(true);
    expect(heard.some((call) => call.url === "/rest/v1/rpc/stetwise_commit_file")).toBe(true);

    process.env.STETWISE_CAP_TRIAL_FILES = "1";
    await expect(store.commitNewFile({ ownerId: USER, filename: "note.docx", kind: "docx", bytes })).rejects.toBeInstanceOf(QuotaError);

    const purged = await store.purge();
    expect(purged.files).toBe(1);
    expect(purged.blobs).toBe(3);
    const deleted = heard.filter((call) => call.method === "DELETE");
    const paths = JSON.parse(deleted[0].body.toString("utf8")) as string[];
    expect(paths).toContain(`${USER}/${"a".repeat(64)}`);
    expect(paths).toContain(`${USER}/${"b".repeat(64)}`);
    expect(paths).toContain(`${USER}/stage/00000000-0000-4000-8000-0000000000cc`);
    expect(paths.some((item) => item.includes("/incoming/"))).toBe(false);
    delete process.env.STETWISE_CAP_TRIAL_FILES;
  });

  it("signs upload and download URLs and copies an object without reading bytes", async () => {
    const { server: httpServer, heard } = listen((req, res, body) => {
      const url = req.url || "";
      if (req.method === "POST" && url.startsWith("/storage/v1/object/sign/")) {
        res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({
          signedURL: `/object/sign/stetwise-files/${USER}/${"d".repeat(64)}?token=download-token`
        }));
        return;
      }
      if (req.method === "POST" && url.startsWith("/storage/v1/object/upload/sign/")) {
        res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({
          url: `/object/upload/sign/stetwise-files/${USER}/incoming/${"e".repeat(32)}`,
          token: "upload-token"
        }));
        return;
      }
      if (req.method === "POST" && url === "/storage/v1/object/copy") {
        res.writeHead(200, { "content-type": "application/json" }).end(body.length ? "{}" : "{}");
        return;
      }
      res.writeHead(404).end("{}");
    });
    server = httpServer;
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    process.env.SUPABASE_URL = `http://127.0.0.1:${port}`;
    process.env.SUPABASE_SERVICE_ROLE_KEY = "service-role-test";
    const store = new SupabaseStore();
    const content = `${USER}/${"d".repeat(64)}`;
    const incoming = `${USER}/incoming/${"e".repeat(32)}`;
    const stage = `${USER}/stage/00000000-0000-4000-8000-0000000000dd`;
    const download = await store.signedDownload(content);
    expect(download.url).toBe(`http://127.0.0.1:${port}/storage/v1/object/sign/stetwise-files/${content}?token=download-token`);
    const upload = await store.signedUpload(incoming);
    expect(upload.url).toContain(`/storage/v1/object/upload/sign/stetwise-files/${incoming}`);
    expect(upload.url).toContain("token=upload-token");
    await store.copyObject(stage, content);
    const copy = heard.find((call) => call.url === "/storage/v1/object/copy");
    expect(JSON.parse(copy!.body.toString("utf8"))).toEqual({
      bucketId: "stetwise-files",
      sourceKey: stage,
      destinationKey: content
    });
    expect(heard.some((call) => call.method === "GET" && call.url.includes("/storage/v1/object/stetwise-files/"))).toBe(false);
  });

  it("declares the private bucket, retention window, and service-role functions", () => {
    const sql = fs.readFileSync(path.join(repoRoot(), "supabase/schema.sql"), "utf8");
    expect(sql).toContain("stetwise-files");
    expect(sql).toContain("file_size_limit");
    expect(sql).toContain("26214400");
    expect(sql).toContain("alter table public.support_requests enable row level security");
    expect(sql).toContain("revoke all on table public.support_requests from public, anon, authenticated");
    expect(sql).not.toMatch(/grant [^;]* on table public\.support_requests to anon/i);
    expect(sql).toContain("set search_path = public, pg_temp");
    expect(sql).toContain("false");
    expect(sql).toContain("retention_hours in (24, 168)");
    for (const name of ["stetwise_commit_file", "stetwise_publish_edit", "stetwise_restore", "stetwise_purge", "stetwise_set_retention"]) {
      expect(sql).toContain(name);
    }
    expect(sql).toContain("revoke all on function");
    expect(sql).toContain("service_role");
    expect(sql).not.toMatch(/(?<![A-Za-z$])\$\d/);
  });
});
