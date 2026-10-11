import { execFileSync } from "node:child_process";
import fs from "node:fs";
import type { Server } from "node:http";
import { once } from "node:events";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { appBaseUrl, dataDir, DEV_USER_ID, repoRoot } from "../src/config.js";
import { app } from "../src/server.js";
import { getStore, resetStoreForTests } from "../src/store.js";

const SENTINEL = "SENTINEL-stetwise-9f3c1a";
const logs: string[] = [];
const originalLog = console.log;
let server: Server;
let base = "";

type ToolData = { isError: boolean; data: Record<string, unknown>; status: number; text: string };

async function rpc(method: string, params?: unknown, id?: number, auth = false) {
  const body: Record<string, unknown> = { jsonrpc: "2.0", method };
  if (id !== undefined) body.id = id;
  if (params !== undefined) body.params = params;
  const response = await fetch(`${base}/mcp`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      ...(auth ? { authorization: "Bearer dev-stetwise" } : {})
    },
    body: JSON.stringify(body)
  });
  const text = await response.text();
  return { status: response.status, text, json: text ? JSON.parse(text) : null };
}

async function call(name: string, args: Record<string, unknown>): Promise<ToolData> {
  const payload = await rpc("tools/call", { name, arguments: args }, 10, true);
  const result = payload.json?.result;
  const data = (result?.structuredContent?.data ?? (result?.content?.[0]?.text ? JSON.parse(result.content[0].text) : {})) as Record<string, unknown>;
  return { isError: Boolean(result?.isError), data, status: payload.status, text: payload.text };
}

async function upload(filename: string, bytes: Buffer) {
  const started = await call("start_upload", { filename });
  expect(started.isError, started.text).toBe(false);
  const response = await fetch(String(started.data.upload_url), {
    method: "PUT",
    headers: { "content-type": "application/octet-stream" },
    body: new Uint8Array(bytes)
  });
  const saved = (await response.json()) as { file_id?: string; error?: { reason?: string } };
  expect(response.status, JSON.stringify(saved)).toBe(200);
  return String(saved.file_id);
}

beforeAll(async () => {
  console.log = (...args: unknown[]) => {
    logs.push(args.map(String).join(" "));
    originalLog(...args);
  };
  resetStoreForTests();
  execFileSync("python3", ["engine/tests/build_fixtures.py", "tests/fixtures/generated"], { cwd: repoRoot() });
  await getStore().ensureDevProfile();
  server = app.listen(0);
  await once(server, "listening");
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  base = `http://127.0.0.1:${port}`;
  process.env.APP_BASE_URL = base;
});

afterAll(async () => {
  console.log = originalLog;
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

describe("MCP", () => {
  it("lists tools without a session", async () => {
    const init = await rpc("initialize", {
      protocolVersion: "2025-03-26",
      capabilities: {},
      clientInfo: { name: "stetwise-test", version: "0" }
    }, 1);
    expect(init.status).toBe(200);
    const listed = await rpc("tools/list", {}, 2);
    expect(listed.status).toBe(200);
    const names = (listed.json.result.tools as { name: string }[]).map((tool) => tool.name);
    for (const name of ["inspect", "read", "search", "propose_edit", "apply_edit", "list_versions", "restore_version", "export", "start_upload", "complete_upload", "import_file", "create_file", "list_comments", "get_usage", "set_retention", "recalculate"]) {
      expect(names).toContain(name);
    }
    const importer = (listed.json.result.tools as { name: string; _meta?: Record<string, unknown> }[]).find((tool) => tool.name === "import_file");
    expect(importer?._meta?.["openai/fileParams"]).toEqual(["file"]);
  });

  it("proposes, applies, and undoes a cell edit back to the original bytes", async () => {
    const original = fs.readFileSync(path.join(repoRoot(), "tests/fixtures/generated/model.xlsx"));
    const fileId = await upload("model.xlsx", original);
    const inspected = await call("inspect", { file_id: fileId });
    expect(inspected.isError, inspected.text).toBe(false);
    expect(JSON.stringify(inspected.data)).toContain("Inputs");

    const proposed = await call("propose_edit", {
      file_id: fileId,
      ops: [{ op: "set_cells", sheet: "Inputs", cells: [{ ref: "A2", value: SENTINEL }] }]
    });
    expect(proposed.isError, proposed.text).toBe(false);
    expect(proposed.data.base_version).toMatch(/^[a-f0-9]{64}$/);
    expect(JSON.stringify(proposed.data.diff)).toContain(SENTINEL);

    const applied = await call("apply_edit", {
      proposal_id: proposed.data.proposal_id,
      base_version: proposed.data.base_version
    });
    expect(applied.isError, applied.text).toBe(false);
    expect(applied.data.version).toBe(2);
    expect(applied.data.idempotent).toBe(false);

    const again = await call("apply_edit", {
      proposal_id: proposed.data.proposal_id,
      base_version: proposed.data.base_version
    });
    expect(again.isError, again.text).toBe(false);
    expect(again.data.idempotent).toBe(true);

    const exported = await call("export", { file_id: fileId });
    expect(exported.isError, exported.text).toBe(false);
    const changed = Buffer.from(await (await fetch(String(exported.data.download_url))).arrayBuffer());
    expect(changed.equals(original)).toBe(false);
    const readBack = await call("read", { file_id: fileId, sheet: "Inputs", range: "A2" });
    expect(readBack.isError, readBack.text).toBe(false);
    expect(JSON.stringify(readBack.data)).toContain(SENTINEL);

    const restored = await call("restore_version", { file_id: fileId, version: 1 });
    expect(restored.isError, restored.text).toBe(false);
    const after = await call("export", { file_id: fileId });
    const undone = Buffer.from(await (await fetch(String(after.data.download_url))).arrayBuffer());
    expect(undone.equals(original)).toBe(true);

    const versions = await call("list_versions", { file_id: fileId });
    const rows = versions.data.versions as { version: number; sha256: string; op: string }[];
    expect(rows.map((row) => row.op)).toEqual(["import", "apply", "restore"]);
    expect(rows[2].sha256).toBe(rows[0].sha256);

    const audit = fs.readFileSync(path.join(dataDir(), "audit.jsonl"), "utf8");
    expect(audit).not.toContain(SENTINEL);
    expect(logs.join("\n")).not.toContain(SENTINEL);
    expect(appBaseUrl()).toBe(base);
  });

  it("rejects a stale base_version and a pivot insert", async () => {
    const original = fs.readFileSync(path.join(repoRoot(), "tests/fixtures/generated/model.xlsx"));
    const fileId = await upload("model.xlsx", original);
    const first = await call("propose_edit", {
      file_id: fileId,
      ops: [{ op: "set_cells", sheet: "Inputs", cells: [{ ref: "A1", value: 1 }] }]
    });
    const second = await call("propose_edit", {
      file_id: fileId,
      ops: [{ op: "set_cells", sheet: "Inputs", cells: [{ ref: "A1", value: 2 }] }]
    });
    expect((await call("apply_edit", { proposal_id: first.data.proposal_id, base_version: first.data.base_version })).isError).toBe(false);
    const stale = await call("apply_edit", { proposal_id: second.data.proposal_id, base_version: second.data.base_version });
    expect(stale.isError).toBe(true);
    expect(JSON.stringify(stale.data)).toContain("base_version does not match the current file");

    const pivotId = await upload("ExcelPivotTableSample.xlsx", fs.readFileSync(path.join(repoRoot(), "tests/fixtures/poi/ExcelPivotTableSample.xlsx")));
    const refused = await call("propose_edit", {
      file_id: pivotId,
      ops: [{ op: "insert_rows", sheet: "Tabelle1", row: 2, count: 1 }]
    });
    expect(refused.isError).toBe(true);
    expect(JSON.stringify(refused.data).toLowerCase()).toContain("pivot");
  });

  it("refuses a private download URL", async () => {
    const refused = await call("import_file", {
      file: { download_url: "http://127.0.0.1/secret.xlsx", file_name: "secret.xlsx" }
    });
    expect(refused.isError).toBe(true);
    expect(JSON.stringify(refused.data)).toContain("ssrf");
  });

  it("blocks the next applied edit, file, and recalculation at the cap", async () => {
    const store = getStore();
    await store.replaceUsage(DEV_USER_ID, { edits: 0, files: 0, recalculations: 0 });
    process.env.STETWISE_CAP_TRIAL_EDITS = "1";
    const original = fs.readFileSync(path.join(repoRoot(), "tests/fixtures/generated/model.xlsx"));
    const fileId = await upload("model.xlsx", original);
    const proposed = await call("propose_edit", {
      file_id: fileId,
      ops: [{ op: "set_cells", sheet: "Inputs", cells: [{ ref: "A2", value: 9 }] }]
    });
    expect((await call("apply_edit", { proposal_id: proposed.data.proposal_id, base_version: proposed.data.base_version })).isError).toBe(false);
    const next = await call("propose_edit", {
      file_id: fileId,
      ops: [{ op: "set_cells", sheet: "Inputs", cells: [{ ref: "A2", value: 8 }] }]
    });
    const blocked = await call("apply_edit", { proposal_id: next.data.proposal_id, base_version: next.data.base_version });
    expect(blocked.isError).toBe(true);
    expect(JSON.stringify(blocked.data)).toContain("does not bill overage");
    delete process.env.STETWISE_CAP_TRIAL_EDITS;

    process.env.STETWISE_CAP_TRIAL_FILES = "1";
    await store.replaceUsage(DEV_USER_ID, { files: 1 });
    const second = await call("start_upload", { filename: "model.xlsx" });
    expect(second.isError).toBe(true);
    expect(JSON.stringify(second.data)).toContain("files");
    delete process.env.STETWISE_CAP_TRIAL_FILES;

    process.env.STETWISE_CAP_TRIAL_RECALCS = "0";
    const recalc = await call("recalculate", { file_id: fileId });
    expect(recalc.isError).toBe(true);
    expect(JSON.stringify(recalc.data)).toContain("recalculations");
    expect(JSON.stringify(recalc.data)).toContain("does not bill overage");
    delete process.env.STETWISE_CAP_TRIAL_RECALCS;
    process.env.STETWISE_CAP_TRIAL_RECALCS = "20";
    const preview = await call("recalculate", { file_id: fileId });
    expect(preview.isError, preview.text).toBe(false);
    expect(JSON.stringify(preview.data)).toContain("LibreOffice");
    const usage = await call("get_usage", {});
    expect((usage.data.recalculations as { used: number }).used).toBe(1);
    if (preview.data.proposal_id) {
      const applied = await call("apply_edit", { proposal_id: preview.data.proposal_id, base_version: preview.data.base_version });
      expect(applied.isError, applied.text).toBe(false);
      const undone = await call("restore_version", { file_id: fileId, version: 1 });
      expect(undone.isError, undone.text).toBe(false);
    }
    delete process.env.STETWISE_CAP_TRIAL_RECALCS;
  });

  it("counts inspect, read, and search toward the read cap", async () => {
    await getStore().replaceUsage(DEV_USER_ID, { reads: 0 });
    const original = fs.readFileSync(path.join(repoRoot(), "tests/fixtures/generated/model.xlsx"));
    const fileId = await upload("model.xlsx", original);
    process.env.STETWISE_CAP_TRIAL_READS = "2";
    try {
      expect((await call("inspect", { file_id: fileId })).isError).toBe(false);
      expect((await call("read", { file_id: fileId, sheet: "Inputs", range: "A1:A3" })).isError).toBe(false);
      const blocked = await call("search", { file_id: fileId, query: "Note" });
      expect(blocked.isError).toBe(true);
      expect(JSON.stringify(blocked.data)).toContain("reads");
      const usage = await call("get_usage", {});
      expect((usage.data.reads as { used: number }).used).toBe(2);
    } finally {
      delete process.env.STETWISE_CAP_TRIAL_READS;
    }
  });

  it("counts a recalculation when LibreOffice starts and leaves no soffice behind", async () => {
    const store = getStore();
    await store.replaceUsage(DEV_USER_ID, { recalculations: 0, proposals: 0 });
    const original = fs.readFileSync(path.join(repoRoot(), "tests/fixtures/generated/model.xlsx"));
    const fileId = await upload("model.xlsx", original);
    const profiles = () =>
      new Set(
        execFileSync("bash", ["-lc", "ls -d /tmp/stetwise-lo-* 2>/dev/null || true"], { encoding: "utf8" })
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean)
      );
    const beforeProfiles = profiles();
    process.env.STETWISE_RECALC_TIMEOUT = "1";
    process.env.STETWISE_RECALC_HOLD = "12";
    try {
      const failed = await call("recalculate", { file_id: fileId });
      expect(failed.isError).toBe(true);
      expect(JSON.stringify(failed.data)).toContain("recalc_failed");
      const usage = await call("get_usage", {});
      expect((usage.data.recalculations as { used: number }).used).toBe(1);
    } finally {
      delete process.env.STETWISE_RECALC_TIMEOUT;
      delete process.env.STETWISE_RECALC_HOLD;
    }
    const deadline = Date.now() + 5000;
    let left = "stetwise-lo-";
    while (Date.now() < deadline) {
      const listing = execFileSync("ps", ["-eo", "args"], { encoding: "utf8" });
      left = listing
        .split("\n")
        .filter((line) => line.includes("stetwise-lo-"))
        .join("\n");
      if (!left) break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    expect(left).toBe("");
    const created = [...profiles()].filter((name) => !beforeProfiles.has(name));
    expect(created).toEqual([]);
  });

  it("creates a blank workbook and fills it through propose and apply", async () => {
    const created = await call("create_file", { kind: "xlsx", filename: "notes.xlsx" });
    expect(created.isError, created.text).toBe(false);
    const fileId = String(created.data.file_id);
    const proposed = await call("propose_edit", {
      file_id: fileId,
      ops: [{ op: "set_cells", sheet: "Sheet1", cells: [{ ref: "A1", value: "Ready" }] }]
    });
    expect(proposed.isError, proposed.text).toBe(false);
    const applied = await call("apply_edit", { proposal_id: proposed.data.proposal_id, base_version: proposed.data.base_version });
    expect(applied.isError, applied.text).toBe(false);
    const readBack = await call("read", { file_id: fileId, sheet: "Sheet1", range: "A1" });
    expect(readBack.isError, readBack.text).toBe(false);
    expect(JSON.stringify(readBack.data)).toContain("Ready");
    const undone = await call("restore_version", { file_id: fileId, version: 1 });
    expect(undone.isError, undone.text).toBe(false);
  });

  it("keeps files 24 hours by default and can extend that to 7 days", async () => {
    const original = fs.readFileSync(path.join(repoRoot(), "tests/fixtures/generated/model.xlsx"));
    const fileId = await upload("retention.xlsx", original);
    const listed = await call("list_files", {});
    const row = (listed.data.files as { file_id: string; expires_at: string; last_used_at: string }[]).find((item) => item.file_id === fileId);
    expect(row).toBeTruthy();
    const gap = Date.parse(row!.expires_at) - Date.parse(row!.last_used_at);
    expect(gap).toBe(24 * 60 * 60 * 1000);
    const updated = await call("set_retention", { hours: 168 });
    expect(updated.isError, updated.text).toBe(false);
    const again = await call("list_files", {});
    const extended = (again.data.files as { file_id: string; expires_at: string; last_used_at: string }[]).find((item) => item.file_id === fileId);
    expect(Date.parse(extended!.expires_at) - Date.parse(extended!.last_used_at)).toBe(168 * 60 * 60 * 1000);
    const refused = await rpc("tools/call", { name: "set_retention", arguments: { hours: 48 } }, 11, true);
    const failed = Boolean(refused.json?.error || refused.json?.result?.isError);
    expect(failed).toBe(true);

    await getStore().expireFile(fileId, "2000-01-01T00:00:00.000Z");
    const purged = await fetch(`${base}/internal/purge`, { method: "POST", headers: { "x-stetwise-purge": "test-purge-secret" } });
    expect(purged.status).toBe(200);
    const body = (await purged.json()) as { files: number };
    expect(body.files).toBeGreaterThan(0);
    const gone = await call("inspect", { file_id: fileId });
    expect(gone.isError).toBe(true);
  });

  it("points upload and download links at the file host and refuses bodies on the edge", async () => {
    const original = fs.readFileSync(path.join(repoRoot(), "tests/fixtures/generated/model.xlsx"));
    const fileId = await upload("host.xlsx", original);
    process.env.STETWISE_FILE_BASE_URL = "https://files.example.test";
    try {
      const exported = await call("export", { file_id: fileId });
      expect(exported.isError, exported.text).toBe(false);
      expect(String(exported.data.download_url)).toMatch(/^https:\/\/files\.example\.test\/v1\/download\//);
      const started = await call("start_upload", { filename: "next.xlsx" });
      expect(started.isError, started.text).toBe(false);
      expect(String(started.data.upload_url)).toMatch(/^https:\/\/files\.example\.test\/v1\/upload\//);
      process.env.STETWISE_ACCEPT_UPLOADS = "0";
      const uploadToken = new URL(String(started.data.upload_url)).pathname.split("/").pop();
      const refused = await fetch(`${base}/v1/upload/${uploadToken}`, { method: "PUT", body: new Uint8Array([1, 2, 3]) });
      expect(refused.status).toBe(421);
      const downloadToken = new URL(String(exported.data.download_url)).pathname.split("/").pop();
      const blocked = await fetch(`${base}/v1/download/${downloadToken}`);
      expect(blocked.status).toBe(421);
    } finally {
      delete process.env.STETWISE_FILE_BASE_URL;
      delete process.env.STETWISE_ACCEPT_UPLOADS;
    }
  });
});
