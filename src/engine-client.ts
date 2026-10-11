import crypto from "node:crypto";
import { spawn } from "node:child_process";
import path from "node:path";
import { repoRoot } from "./config.js";
import { ToolError } from "./errors.js";

export type EngineFailure = { ok: false; error: { code: string; reason: string } };
export type EngineSuccess<T> = { ok: true; result: T };
export type EngineResponse<T> = EngineSuccess<T> | EngineFailure;

const TIMEOUT_MS = 120_000;

export async function engineCall<T>(payload: Record<string, unknown>): Promise<T> {
  const response = process.env.ENGINE_URL ? await callHttp(payload) : await callCli(payload);
  if (!response || typeof response !== "object" || !("ok" in response)) {
    throw new ToolError("The file engine returned an unreadable result. The file was not changed.", "engine_error");
  }
  const body = response as EngineResponse<T>;
  if (!body.ok) {
    const code = body.error?.code || "refused";
    const reason = body.error?.reason || "The file engine refused this request. The file was not changed.";
    throw new ToolError(reason, code);
  }
  return body.result;
}

async function callHttp(payload: Record<string, unknown>): Promise<unknown> {
  const secret = process.env.ENGINE_HMAC_SECRET || "";
  if (!secret) throw new ToolError("ENGINE_HMAC_SECRET is not set.", "engine_unconfigured");
  const body = Buffer.from(JSON.stringify(payload));
  const signature = crypto.createHmac("sha256", secret).update(body).digest("hex");
  const url = `${process.env.ENGINE_URL!.replace(/\/$/, "")}/v1/dispatch`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", "x-stetwise-signature": signature },
      body,
      signal: AbortSignal.timeout(TIMEOUT_MS)
    });
  } catch {
    throw new ToolError("The file engine could not be reached. The file was not changed.", "engine_unavailable");
  }
  try {
    return await response.json();
  } catch {
    throw new ToolError("The file engine returned an unreadable result. The file was not changed.", "engine_error");
  }
}

function callCli(payload: Record<string, unknown>): Promise<unknown> {
  const python = process.env.STETWISE_PYTHON || "python3";
  const root = repoRoot();
  return new Promise((resolve, reject) => {
    const child = spawn(python, ["-m", "stetwise"], {
      cwd: root,
      env: { ...process.env, PYTHONPATH: path.join(root, "engine") },
      stdio: ["pipe", "pipe", "pipe"]
    });
    const stdout: Buffer[] = [];
    let settled = false;
    const timer = setTimeout(() => {
      child.kill();
      finish(new ToolError("The file engine timed out. The file was not changed.", "engine_timeout"));
    }, TIMEOUT_MS);
    const finish = (error?: Error, value?: unknown) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else resolve(value);
    };
    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on("data", () => undefined);
    child.on("error", () => finish(new ToolError("The file engine could not be started. The file was not changed.", "engine_unavailable")));
    child.on("close", () => {
      try {
        finish(undefined, JSON.parse(Buffer.concat(stdout).toString("utf8")));
      } catch {
        finish(new ToolError("The file engine returned an unreadable result. The file was not changed.", "engine_error"));
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}
