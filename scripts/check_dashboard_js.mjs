import fs from "node:fs";

const html = fs.readFileSync("dashboard/index.html", "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
  .map((m) => m[1].trim())
  .filter(Boolean);

if (!scripts.length) {
  throw new Error("No inline dashboard JavaScript found.");
}

for (const source of scripts) {
  new Function(source);
}

console.log(`Dashboard JavaScript syntax OK (${scripts.length} inline script block(s)).`);
