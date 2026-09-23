import fs from "node:fs";

const app = fs.readFileSync("dashboard/app.js", "utf8");
new Function(app);

const html = fs.readFileSync("dashboard/index.html", "utf8");
if (!html.includes('src="/app.js"')) {
  throw new Error("dashboard/index.html is not loading /app.js");
}

console.log("Dashboard JavaScript syntax OK.");
