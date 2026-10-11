import { describe, expect, it } from "vitest";
import { isBlockedAddress, isBlockedHostname } from "../src/ssrf.js";

describe("download host checks", () => {
  it("refuses loopback, private, link-local, and local names", () => {
    for (const host of ["127.0.0.1", "10.1.2.3", "192.168.0.4", "172.16.5.5", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1"]) {
      expect(isBlockedAddress(host) || isBlockedHostname(host)).toBe(true);
    }
    for (const name of ["localhost", "metadata.google.internal", "printer.local"]) {
      expect(isBlockedHostname(name)).toBe(true);
    }
    expect(isBlockedHostname("example.com")).toBe(false);
  });
});
