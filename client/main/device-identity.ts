/**
 * 设备标识（本地持久化指纹）：注册赠分防刷（契约 RegisterCreate.fingerprint minLength 8）。
 *
 * 生成与存储约定：
 * - 首次运行生成 128 位随机指纹（32 位十六进制）并落盘（userData/device-fingerprint）；
 * - 服务端按指纹判定「同设备重复注册不再赠分」，故指纹需跨重启稳定；
 * - 只存随机标识、不采集硬件信息（隐私最小化）；读取非法/写入失败时降级为
 *   会话内指纹（功能可用，仅防刷粒度下降），不阻断登录注册。
 *
 * 本模块不依赖 electron（node:test 可直接加载；存储路径由调用方注入）。
 */
import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

/** 契约约束：fingerprint minLength 8 / maxLength 64。 */
export const FINGERPRINT_MIN_LENGTH = 8;
export const FINGERPRINT_MAX_LENGTH = 64;

/** 指纹长度合法性（注册前校验与读取校验共用）。 */
export function isValidFingerprint(value: string): boolean {
  const trimmed = value.trim();
  return trimmed.length >= FINGERPRINT_MIN_LENGTH && trimmed.length <= FINGERPRINT_MAX_LENGTH;
}

/** 生成新指纹（128 位随机 → 32 位十六进制，满足契约长度区间）。 */
export function newFingerprint(): string {
  return randomBytes(16).toString("hex");
}

/**
 * 读取设备指纹；文件不存在 / 内容非法 / 不可读时重新生成并尽力落盘。
 * 落盘失败只降级（返回本次生成值），不抛错——登录注册不因本地存储受限而不可用。
 */
export function ensureDeviceFingerprint(filePath: string): string {
  try {
    const stored = readFileSync(filePath, "utf-8").trim();
    if (isValidFingerprint(stored)) return stored;
  } catch {
    // 首次运行（文件不存在）或读取失败：进入生成分支
  }
  const fingerprint = newFingerprint();
  try {
    mkdirSync(path.dirname(filePath), { recursive: true });
    writeFileSync(filePath, fingerprint, { encoding: "utf-8", mode: 0o600 });
  } catch {
    // 只读/受限环境：降级为会话内指纹
  }
  return fingerprint;
}