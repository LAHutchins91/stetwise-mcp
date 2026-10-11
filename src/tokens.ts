import crypto from "node:crypto";
import { signingSecret } from "./config.js";
import { ToolError } from "./errors.js";

export type UploadToken = {
  typ: "put";
  fileId: string;
  ownerId: string;
  filename: string;
  exp: number;
  nonce: string;
};

export type DownloadToken = {
  typ: "get";
  fileId: string;
  ownerId: string;
  sha: string;
  exp: number;
};

export function signToken(payload: UploadToken | DownloadToken): string {
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  const mac = crypto.createHmac("sha256", signingSecret()).update(body).digest("base64url");
  return `${body}.${mac}`;
}

export function readToken(token: string): UploadToken | DownloadToken {
  const dot = token.indexOf(".");
  if (dot <= 0) throw new ToolError("This link is not valid.", "bad_link");
  const body = token.slice(0, dot);
  const mac = token.slice(dot + 1);
  const expected = crypto.createHmac("sha256", signingSecret()).update(body).digest("base64url");
  const given = Buffer.from(mac);
  const want = Buffer.from(expected);
  if (given.length !== want.length || !crypto.timingSafeEqual(given, want)) {
    throw new ToolError("This link is not valid.", "bad_link");
  }
  try {
    return JSON.parse(Buffer.from(body, "base64url").toString("utf8")) as UploadToken | DownloadToken;
  } catch {
    throw new ToolError("This link is not valid.", "bad_link");
  }
}

export function assertFresh(exp: number): void {
  if (!Number.isFinite(exp) || exp <= Math.floor(Date.now() / 1000)) {
    throw new ToolError("This link has expired.", "expired_link");
  }
}
