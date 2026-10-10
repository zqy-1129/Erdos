/** 开发解释器与正式捆绑入口分开解析；正式包只执行通过完整性与版本校验的资源。 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { ENGINE_MANIFEST_FILE, parseManifest, verifyEngineDir } from "./tamper-check.ts";

export interface EngineLaunch { command: string; args: string[]; cwd: string }
export interface EngineLaunchOptions {
  packaged: boolean;
  clientRoot: string;
  resourcesPath: string;
  platform: NodeJS.Platform;
  expectedVersion?: string;
  developmentCommand?: string;
}
export class EngineLaunchError extends Error {
  constructor(message: string) { super(message); this.name = "EngineLaunchError"; }
}

/** 旧版单入口 W17 清单可省略 entrypoint；有多个 root exe 时必须显式选择已列入哈希的入口。 */
export function resolveEngineLaunch(options: EngineLaunchOptions): EngineLaunch {
  if (!options.packaged) {
    const repoRoot = resolve(options.clientRoot, "..");
    return { command: options.developmentCommand || join(repoRoot, "engine", ".venv",
      ...(options.platform === "win32" ? ["Scripts", "python.exe"] : ["bin", "python"])),
    args: ["-m", "engine"], cwd: repoRoot };
  }
  const root = join(options.resourcesPath, "engine");
  if (!existsSync(root)) throw new EngineLaunchError("安装包缺少捆绑引擎，请重新安装完整客户端");
  const integrity = verifyEngineDir(root);
  if (!integrity.ok) throw new EngineLaunchError("引擎完整性校验失败：" + integrity.mismatches.join("；"));
  const manifest = parseManifest(readFileSync(join(root, ENGINE_MANIFEST_FILE), "utf8"));
  const versionPath = join(root, "version.txt");
  if (!options.expectedVersion || !Object.hasOwn(manifest.files, "version.txt") || !existsSync(versionPath) ||
      manifest.engineVersion !== options.expectedVersion || readFileSync(versionPath, "utf8").trim() !== options.expectedVersion) {
    throw new EngineLaunchError("捆绑引擎版本与构建绑定不一致，已拒绝启动");
  }
  const candidates = Object.keys(manifest.files).filter(name => !name.includes("/") &&
    (options.platform === "win32" ? /\.exe$/i.test(name) : !name.includes(".")));
  const entrypoint = manifest.entrypoint ?? (candidates.length === 1 ? candidates[0] : undefined);
  if (!entrypoint || (options.platform === "win32" && !/\.exe$/i.test(entrypoint))) {
    throw new EngineLaunchError("引擎入口不明确，需在哈希清单中指定 entrypoint");
  }
  const command = join(root, entrypoint);
  return { command, args: [], cwd: dirname(command) };
}
