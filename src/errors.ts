/** Errors safe to show to an assistant. Messages must not include file contents. */

export class ToolError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly started = false
  ) {
    super(message);
    this.name = "ToolError";
  }
}
