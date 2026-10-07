/**
 * 引擎二进制篡改校验（FE-PKG W18 / SP5-3）：启动时校验捆绑引擎产物的 sha256。
 *
 * 红线（重规划 SP5-3「篡改包哈希校验」）：安装包携带 sha256 清单（manifest），
 * 启动时对 resources/engine/ 下每个文件计算哈希并与清单比对，任一不一致即拒绝启动
 * 并提示（防杀软篡改 / 供应链投毒 / 版本被替换）。
 *
 * 纯函数便于单测；文件读取与拒绝启动的宿主接线在打包集成阶段（W17 产物就位后）接入。
 */
import { createHash } from "node:crypto";

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
