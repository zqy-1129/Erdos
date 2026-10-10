/**
 * 引擎二进制篡改校验（FE-PKG W18 / SP5-3）：启动时校验捆绑引擎产物的 sha256。
 *
 * 红线（重规划 SP5-3「篡改包哈希校验」）：安装包携带 sha256 清单（manifest.json），
 * 启动时对 resources/engine/ 下每个文件计算哈希并与清单比对，任一不一致即拒绝启动
 * 并提示。清单用于资源完整性检查；发布可信性仍依赖安装包签名与可信更新源。
 *
 * 分层：
 * - 纯函数（sha256/verifyIntegrity/parseManifest）→ node:test 单测；
 * - 文件系统层（collectDirHashes/verifyEngineDir）→ 宿主在 app 启动时调用（见 main/index.ts），
 *   开发期 resources/engine 不存在则跳过（与 verify-engine-version.mjs 同口径）。
 */
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, lstatSync, realpathSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";

/** 篡改校验清单：相对路径 → 预期 sha256（hex，64 字符）。 */
export interface IntegrityManifest {
  /** 清单格式版本（未来升级兼容）。 */
  version: 1;
  /** 引擎版本（与应用版本绑定，见 verify-engine-version）。 */
  engineVersion: string;
  /** 多个启动文件时显式指定入口；路径必须在 files 哈希清单内。 */
  entrypoint?: string;
  files: Record<string, string>;
}

/** 校验结果：ok=false 时 mismatches 列出具体不一致项。 */
export interface IntegrityResult {
  ok: boolean;
  mismatches: string[];
}

/** 计算 buffer 的 sha256（hex）。 */
export function sha256(content: Buffer): string {
  return createHash("sha256").update(content).digest("hex");
}

/**
 * 校验实际文件哈希与清单是否一致。
 * - 清单内任一文件缺失 → mismatch「缺失」；
 * - 存在但哈希不等 → mismatch「哈希不一致」；
 * - 清单外的多余文件不判违规（仅校验清单内项，允许打包器额外产物）。
 */
export function verifyIntegrity(
  actual: Record<string, string>,
  manifest: IntegrityManifest,
): IntegrityResult {
  const mismatches: string[] = [];
  for (const [path, expected] of Object.entries(manifest.files)) {
    const got = Object.hasOwn(actual, path) ? actual[path] : undefined;
    if (got === undefined) {
      mismatches.push(`${path}: 缺失`);
    } else if (got !== expected) {
      mismatches.push(`${path}: 哈希不一致`);
    }
  }
  return { ok: mismatches.length === 0, mismatches };
}

/** 读取 manifest JSON 并做结构校验（损坏/缺字段 → 拒绝，宁严勿松）。 */
export function parseManifest(raw: string): IntegrityManifest {
  if (raw.length > 5 * 1024 * 1024) throw new Error("篡改校验清单过大");
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("篡改校验清单损坏：非法 JSON");
  }
  const obj = parsed as Partial<IntegrityManifest>;
  if (typeof obj !== "object" || obj === null || typeof obj.files !== "object" || obj.files === null || Array.isArray(obj.files)) {
    throw new Error("篡改校验清单结构非法：缺少 files 字段");
  }
  if ((obj.version !== undefined && obj.version !== 1) || typeof obj.engineVersion !== "string" ||
      !/^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(obj.engineVersion)) throw new Error("篡改校验清单版本无效");
  const entries = Object.entries(obj.files);
  if (!entries.length || entries.length > 20000 || entries.some(([name, hash]) => !validPath(name) ||
      typeof hash !== "string" || !/^[a-f0-9]{64}$/i.test(hash))) throw new Error("篡改校验清单路径或哈希无效");
  if (obj.entrypoint !== undefined && (typeof obj.entrypoint !== "string" || !validPath(obj.entrypoint) ||
      !Object.hasOwn(obj.files, obj.entrypoint))) throw new Error("引擎入口未纳入哈希清单");
  return { version: 1, engineVersion: obj.engineVersion,
    files: Object.fromEntries(entries.map(([name, hash]) => [name, hash.toLowerCase()])),
    ...(obj.entrypoint === undefined ? {} : { entrypoint: obj.entrypoint }) };
}

/** 清单采用跨平台相对路径，拒绝父目录、Windows ADS 和路径别名。 */
function validPath(value: string): boolean {
  return value.length > 0 && value.length <= 1024 && !/[\\:]/.test(value) && !Array.from(value).some(c => c.charCodeAt(0) < 32) &&
    value.split("/").every(part => !!part && part !== "." && part !== ".." && !/[. ]$/.test(part));
}

/** 清单文件名（引擎打包脚本在 resources/engine/ 下生成）。 */
export const ENGINE_MANIFEST_FILE = "manifest.json";

/**
 * 递归收集目录内文件的 sha256（相对路径 → hex）。
 * 相对路径统一用 `/` 分隔（跨平台清单可比对：Windows 打包产物与 CI 校验同口径）。
 */
export function collectDirHashes(dir: string): Record<string, string> {
  const out: Record<string, string> = Object.create(null) as Record<string, string>;
  const walk = (current: string): void => {
    for (const name of readdirSync(current)) {
      const full = join(current, name);
      const info = lstatSync(full);
      if (info.isSymbolicLink()) throw new Error("引擎资源不允许符号链接");
      if (info.isDirectory()) {
        walk(full);
      } else {
        out[relative(dir, full).split(sep).join("/")] = sha256(readFileSync(full));
      }
    }
  };
  walk(dir);
  return out;
}

/**
 * 引擎目录级篡改校验（启动前调用，fail-closed）：
 * - 清单缺失/损坏 → 视为校验失败（无法证明完整性 → 拒绝启动）；
 * - 清单内任一文件缺失/哈希不一致 → 失败并列明；
 * - 清单外多余文件不判违规（允许打包器额外产物）。
 */
export function verifyEngineDir(engineDir: string, manifestFileName: string = ENGINE_MANIFEST_FILE): IntegrityResult {
  let raw: string;
  try {
    raw = readFileSync(join(engineDir, manifestFileName), "utf-8");
  } catch {
    return { ok: false, mismatches: [`${manifestFileName}: 清单缺失（无法校验完整性，拒绝启动）`] };
  }
  let manifest: IntegrityManifest;
  try {
    manifest = parseManifest(raw);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { ok: false, mismatches: [`${manifestFileName}: ${message}`] };
  }
  try {
    // 只读取清单所列资源，额外大文件不影响启动耗时；O(已列资源总字节数)。
    const root = realpathSync(engineDir);
    const actual: Record<string, string> = Object.create(null) as Record<string, string>;
    for (const name of Object.keys(manifest.files)) {
      const full = resolve(root, name);
      try {
        let component = root;
        for (const part of name.split("/")) {
          component = join(component, part);
          if (lstatSync(component).isSymbolicLink()) throw new Error("引擎资源不允许符号链接");
        }
        const rel = relative(root, realpathSync(full));
        if (rel === ".." || rel.startsWith(".." + sep) || !lstatSync(full).isFile()) throw new Error("引擎资源边界无效");
        actual[name] = sha256(readFileSync(full));
      } catch (error) {
        if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") continue;
        throw error;
      }
    }
    return verifyIntegrity(actual, manifest);
  } catch {
    return { ok: false, mismatches: ["引擎资源无法读取或含符号链接，拒绝启动"] };
  }
}
