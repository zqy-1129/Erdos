/**
 * 自动更新策略（FE-PKG W18 / SP5-3 / F-102）：渠道门禁 + 版本判定（纯函数，可单测）。
 *
 * 对齐《客户端开发详细方案》§9 与 F-102 产品约束：
 * - preview 渠道**禁用自动更新**（产品约束）；dev 渠道不发起更新（本地调试）；
 * - alpha / production 允许自动更新；
 * - 版本清单可含强制升级阈值（minimumSupported，客户端架构 §5.6）；
 * - 引擎与应用版本绑定发布，禁止运行时动态升级（见 scripts/verify-engine-version.mjs）。
 *
 * 本模块不依赖 electron 运行时；electron-updater 的实际接线见 main/update/updater.ts。
 */

export type UpdateChannel = "dev" | "preview" | "alpha" | "production";

/** 渠道解析：ERDOS_CHANNEL 显式指定优先；否则按是否 dev 运行回落。 */
export function resolveChannel(env: Record<string, string | undefined>, isDev: boolean): UpdateChannel {
  const raw = (env["ERDOS_CHANNEL"] ?? "").trim().toLowerCase();
  if (raw === "preview" || raw === "alpha" || raw === "production") return raw;
  return isDev ? "dev" : "production";
}

/** 渠道是否允许自动更新检查（preview/dev 禁用）。 */
export function isAutoUpdateEnabled(channel: UpdateChannel): boolean {
  return channel === "alpha" || channel === "production";
}

/** 版本段解析：容忍 v 前缀与 -suffix（预发布后缀不参与数值比较）。 */
function parseVersion(version: string): number[] {
  const core = version.trim().replace(/^v/i, "").split("-")[0];
  return core.split(".").map((segment) => {
    const n = Number.parseInt(segment, 10);
    return Number.isFinite(n) ? n : 0;
  });
}

/** 数字段版本比较：a>b → 1；a<b → -1；相等 → 0。 */
export function compareVersions(a: string, b: string): number {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  const length = Math.max(pa.length, pb.length);
  for (let i = 0; i < length; i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x !== y) return x > y ? 1 : -1;
  }
  return 0;
}

/** 版本清单条目（latest.yml 的运行时投影，字段子集）。 */
export interface UpdateCandidate {
  version: string;
  /** 最低支持版本：当前版本低于该值 → 强制升级。 */
  minimumSupported?: string;
}

export type UpdateDecision =
  | { action: "none"; reason: string }
  | { action: "optional"; reason: string }
  | { action: "forced"; reason: string };

/**
 * 更新决策：
 * - 渠道禁用 → none；已最新 → none；
 * - 低于 minimumSupported → forced（阻断继续使用，UI 必须引导升级）；
 * - 否则 → optional（用户确认后下载）。
 */
export function decideUpdate(current: string, candidate: UpdateCandidate, channel: UpdateChannel): UpdateDecision {
  if (!isAutoUpdateEnabled(channel)) {
    return { action: "none", reason: `渠道 ${channel} 禁用自动更新（F-102 约束）` };
  }
  if (compareVersions(candidate.version, current) <= 0) {
    return { action: "none", reason: "已是最新版本" };
  }
  if (candidate.minimumSupported !== undefined && compareVersions(current, candidate.minimumSupported) < 0) {
    return { action: "forced", reason: `当前版本低于最低支持版本 ${candidate.minimumSupported}` };
  }
  return { action: "optional", reason: `发现新版本 ${candidate.version}` };
}