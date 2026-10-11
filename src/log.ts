/** Logs hashes, sizes, and operation names. Never file contents or cell values. */

export function logEvent(event: { op: string; code?: string; sha256?: string; size?: number }): void {
  console.log(
    JSON.stringify({
      service: "stetwise",
      op: event.op,
      code: event.code ?? "ok",
      sha256: event.sha256,
      size: event.size
    })
  );
}
