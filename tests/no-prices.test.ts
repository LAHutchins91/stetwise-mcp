import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { repoRoot } from "../src/config.js";

function files(dir: string, out: string[] = []): string[] {
  for (const name of fs.readdirSync(dir)) {
    if (name === "node_modules" || name === "fixtures" || name === ".git" || name === "dist" || name === "package-lock.json") continue;
    const full = path.join(dir, name);
    if (fs.statSync(full).isDirectory()) files(full, out);
    else if (/\.(ts|py|md|sql|json|txt|example)$/.test(name)) out.push(full);
  }
  return out;
}

describe("prices", () => {
  it("does not print a dollar amount", () => {
    const hits: string[] = [];
    for (const file of files(repoRoot())) {
      const text = fs.readFileSync(file, "utf8");
      // Excel absolute refs such as $A$1 are not currency. A dollar amount is $ plus digits not preceded by a column letter.
      if (/(?<![A-Za-z$])\$\d/.test(text)) hits.push(path.relative(repoRoot(), file));
    }
    expect(hits).toEqual([]);
  });
});
