/**
 * 引擎二进制篡改校验（FE-PKG W18 / SP5-3）：启动时校验捆绑引擎产物的 sha256。
 *
 * 红线（重规划 SP5-3「篡改包哈希校验」）：安装包携带 sha256 清单（manifest.json），
 * 启动时对 resources/engine/ 下每个文件计算哈希并与清单比对，任一不一致即拒绝启动
 * 并提示（防杀软篡改 / 供应链投毒 / 版本被替换）。
 *
 * 分层：
 * - 纯函数（sha256/verifyIntegrity/parseManifest）→ node:test 单测；
 * - 文件系统层（collectDirHashes/verifyEngineDir）→ 宿主在 app 启动时调用（见 main/index.ts），
 *   开发期 resources/engine 不存在则跳过（与 verify-engine-version.mjs 同口径）。
 */
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

/** 篡改校验清单：相对路径 → 预期 sha256（hex，64 字符）。 */
export interface IntegrityManifest {
  /** 清单格式版本（未来升级兼容）。 */
  version: 1;
  /** 引擎版本（与应用版本绑定，见 verify-engine-version）。 */
  engineVersion: string;
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
    const got = actual[path];
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
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("篡改校验清单损坏：非法 JSON");
  }
  const obj = parsed as Partial<IntegrityManifest>;
  if (typeof obj !== "object" || obj === null || typeof obj.files !== "object" || obj.files === null) {
    throw new Error("篡改校验清单结构非法：缺少 files 字段");
  }
  return { version: 1, engineVersion: String(obj.engineVersion ?? ""), files: obj.files as Record<string, string> };
}

/** 清单文件名（引擎打包脚本在 resources/engine/ 下生成）。 */
export const ENGINE_MANIFEST_FILE = "manifest.json";

/**
 * 递归收集目录内文件的 sha256（相对路径 → hex）。
 * 相对路径统一用 `/` 分隔（跨平台清单可比对：Windows 打包产物与 CI 校验同口径）。
 */
export function collectDirHashes(dir: string): Record<string, string> {
  const out: Record<string, string> = {};
  const walk = (current: string): void => {
    for (const name of readdirSync(current)) {
      const full = join(current, name);
      if (statSync(full).isDirectory()) {
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
  return verifyIntegrity(collectDirHashes(engineDir), manifest);
}
