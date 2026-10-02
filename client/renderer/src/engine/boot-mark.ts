/**
 * 冷启动计时（SP3-7 非功能验收前置）：冷启动 <3s 统计基线。
 *
 * - BootTimer 在 entry 最早处打点：boot（脚本执行）→ first-render（React 首屏挂载完成）；
 * - isColdStartWithin 判定预算（默认 3000ms，PRD 6.1 冷启动 <3s）；
 * - 时钟可注入（测试）；设置页展示实测值。
 */

export interface BootMarks {
  [name: string]: number;
}

export class BootTimer {
  private marks: BootMarks = {};
  private readonly now: () => number;

  constructor(now: () => number = () => performance.now()) {
    this.now = now;
  }

  /** 打点（同名只记首次）。 */
  mark(name: string): void {
    if (!(name in this.marks)) {
      this.marks[name] = this.now();
    }
  }

  /** 两点耗时（任一缺失返回 null）。 */
  elapsed(from: string, to: string): number | null {
    const start = this.marks[from];
    const end = this.marks[to];
    if (start === undefined || end === undefined) return null;
    return Math.round((end - start) * 100) / 100;
  }

  /** 冷启动总耗时（boot → first-render）。 */
  appReadyMs(): number | null {
    return this.elapsed("boot", "first-render");
  }

  snapshot(): BootMarks {
    return { ...this.marks };
  }
}

/** 冷启动预算判定（PRD：<3s）。 */
export function isColdStartWithin(readyMs: number | null, budgetMs = 3000): boolean | null {
  if (readyMs === null) return null; // 尚未测量
  return readyMs < budgetMs;
}