/**
 * 引擎版本绑定校验（FE-PKG W18 / SP5-3）：构建时校验捆绑引擎产物版本与
 * engine/pyproject.toml 一致，禁止客户端运行时动态升级引擎大版本。
 *
 * 用法：node scripts/verify-engine-version.mjs
 * 退出码：0 = 通过（或 W17 产物未就位时跳过）；1 = 版本不一致；2 = 产物/读取错误。
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(__dirname, "..", ".."); // client/scripts → client → Edros
const pyprojectPath = join(repoRoot, "engine", "pyproject.toml");
const engineResourcesDir = join(__dirname, "..", "resources", "engine");

function readEngineVersion() {
  const text = readFileSync(pyprojectPath, "utf-8");
  const m = text.match(/^version\s*=\s*"([^"]+)"/m);
  if (!m) throw new Error("engine/pyproject.toml 缺少 version 字段");
  return m[1];
}

const pyprojectVersion = readEngineVersion();

// W17 引擎打包产物未就位：跳过校验（打包阶段由 CI 强制产物存在后再执行）
if (!existsSync(engineResourcesDir)) {
  console.log(
    `[verify-engine-version] resources/engine 未就位（W17 产物待打包），跳过校验。目标版本 ${pyprojectVersion}`,
  );
  process.exit(0);
}

const versionFile = join(engineResourcesDir, "version.txt");
if (!existsSync(versionFile)) {
  console.error("[verify-engine-version] 引擎产物缺少 version.txt（W17 打包脚本应生成），无法校验版本绑定");
  process.exit(2);
}

const engineVersion = readFileSync(versionFile, "utf-8").trim();
if (engineVersion !== pyprojectVersion) {
  console.error(
    `[verify-engine-version] 版本不一致：pyproject=${pyprojectVersion} engine=${engineVersion}（禁运行时升级）`,
  );
  process.exit(1);
}

console.log(`[verify-engine-version] 通过：引擎版本 ${engineVersion} 与 pyproject 一致`);
