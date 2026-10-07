/**
 * 引擎首行密钥注入（FE-KEYIN W5）：spawn 后向 stdin 写首行。
 *
 * 约定（engine-protocol.md §3）：
 * - 空行 → 无 Key 模式（引擎走 FakeLLM 离线演示）；
 * - 非空 → 视为 API Key 一次性注入（引擎读后仅内存持有，不落日志）。
 * 客户端永不写 `ERDOS_NO_KEY` 字面量（该值仅保留给命令行手工调试）。
 */
import type { ChildProcessWithoutNullStreams } from "node:child_process";

/** 就绪判定超时（spawning→ready 判活窗口，host.ts 据此启动计时器）。 */
export const INJECT_READY_TIMEOUT_MS = 10_000;

/** 构造首行：无 Key → 空字符串（空行）；有 Key → 明文原样（仅内存，写毕即弃）。 */
export function buildFirstLine(key: string | null): string {
  return key ?? "";
}

/** 向引擎 stdin 写首行（Key 或空行）+ 换行，写毕由调用方丢弃 Key 引用。 */
export function injectFirstLine(child: ChildProcessWithoutNullStreams, key: string | null): void {
  const firstLine = buildFirstLine(key);
  child.stdin.write(`${firstLine}\n`);
}
