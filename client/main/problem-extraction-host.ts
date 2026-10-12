import { fork } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

export interface ExtractionContext { signal: AbortSignal; onProgress: (progress: number) => void }

/** 仅传 OS/语言与覆盖率运行所需变量，不允许继承 Node 注入选项、模型 Key 或登录令牌。 */
export function extractionEnvironment(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ELECTRON_RUN_AS_NODE: "1" };
  for (const key of ["PATH", "Path", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "NODE_V8_COVERAGE"]) {
    if (source[key]) env[key] = source[key];
  }
  return env;
}

/** 每次解析使用独立进程；取消/超时强制结束该 PID，不继承模型和账号凭据。 */
export async function extractInProcess(extension: string, bytes: Buffer, ocrCachePath: string,
  context: ExtractionContext): Promise<string> {
  context.signal.throwIfAborted();
  const compiled = new URL("./problem-extract-process.js", import.meta.url);
  const entry = existsSync(fileURLToPath(compiled)) ? compiled : new URL("./problem-extract-process.ts", import.meta.url);
  const env = extractionEnvironment(process.env);
  const child = fork(fileURLToPath(entry), [], { env, windowsHide: true,
    serialization: "advanced", stdio: ["ignore", "ignore", "ignore", "ipc"] });
  const stopped = new Promise<void>(resolve => { child.once("exit", () => resolve()); child.once("error", () => resolve()); });
  let cancel: () => void = () => {};
  let completed = false;
  try {
    return await new Promise<string>((resolve, reject) => {
      cancel = () => { child.kill("SIGKILL"); reject(context.signal.reason); };
      context.signal.addEventListener("abort", cancel, { once: true });
      child.once("error", () => reject(new Error("题面解析服务无法启动，请重试")));
      let result: string | undefined;
      let failure: Error | undefined;
      child.once("exit", code => {
        if (code === 0 && (result !== undefined || failure)) {
          completed = true;
          if (failure) reject(failure); else resolve(result!);
        }
        else reject(new Error("题面解析服务异常退出，请重试"));
      });
      child.on("message", (message: { kind?: string; text?: unknown; message?: unknown; progress?: unknown }) => {
        if (context.signal.aborted) return;
        if (message.kind === "progress" && typeof message.progress === "number") context.onProgress(message.progress);
        else if (message.kind === "result" && typeof message.text === "string" && message.text.length <= 60000) result = message.text;
        else if (message.kind === "error" && typeof message.message === "string") failure = new Error(message.message);
        else if (message.kind === "result") failure = new Error("题面超过 6 万字，请拆分后导入");
      });
      child.send({ extension, bytes, ocrCachePath }, error => { if (error) reject(new Error("题面解析服务无法通信，请重试")); });
    });
  } finally {
    context.signal.removeEventListener("abort", cancel);
    if (!completed) child.kill("SIGKILL");
    await stopped;
  }
}
