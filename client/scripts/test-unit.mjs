import { readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
const tests = readdirSync("tests").filter(name => name.endsWith(".test.ts")).sort().map(name => "tests/" + name);
const result = spawnSync(process.execPath, ["--test", ...tests], { stdio: "inherit", env: process.env });
process.exit(result.status ?? 1);
