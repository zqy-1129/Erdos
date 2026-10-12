import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative, resolve, sep } from "node:path";
import { ProblemImporter } from "../main/problem-import.ts";
import { extractInProcess, extractionEnvironment, type ExtractionContext } from "../main/problem-extraction-host.ts";

let temp: string; let path: string;
before(async () => { temp = await mkdtemp(join(tmpdir(), "erdos-import-life-")); path = join(temp, "题面.txt"); await writeFile(path, "fixture text"); });
after(async () => { const rel = relative(resolve(tmpdir()), resolve(temp)); assert.ok(rel && !rel.startsWith(".." + sep)); await rm(temp, { recursive: true, force: true }); });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes; }); return { promise, resolve }; }
const options = () => ({ select: async () => path, ocrCachePath: join(temp, "ocr") });

describe("题面导入生命周期与资源回收", () => {
  it("解析环境只传允许的OS变量，拒绝继承模型/账号秘密和Node执行注入选项", () => {
    assert.deepEqual(extractionEnvironment({ PATH: "fixture-path", TEMP: "fixture-temp", LANG: "zh_CN", LLM_API_KEY: "synthetic-secret",
      ERDOS_ACCESS_TOKEN: "synthetic-token", NODE_OPTIONS: "--eval fixture", ELECTRON_RUN_AS_NODE: "0" }),
    { PATH: "fixture-path", TEMP: "fixture-temp", LANG: "zh_CN", ELECTRON_RUN_AS_NODE: "1" });
  });
  it("进度可查询且单调有界，只有本操作能取消；状态视图不含路径与题面", async () => {
    const ready = deferred<ExtractionContext>(); const text = deferred<string>();
    const importer = new ProblemImporter({ ...options(), extract: async (_ext, _bytes, context) => { ready.resolve(context); return text.promise; } });
    const pending = importer.import("import-normal"); assert.equal(importer.status("import-normal")?.phase, "selecting");
    const context = await ready.promise; assert.equal(importer.status("import-normal")?.phase, "extracting");
    context.onProgress(.6); context.onProgress(.2); context.onProgress(NaN);
    assert.deepEqual(importer.status("import-normal"), { importId: "import-normal", phase: "extracting", progress: .6 });
    const view = importer.status("import-normal")!; view.progress = 99; assert.equal(importer.status("import-normal")?.progress, .6);
    assert.equal(importer.status("import-foreign"), null); assert.deepEqual(importer.cancel("import-foreign"), { cancelled: false });
    await assert.rejects(importer.import("import-second"), /正在进行/);
    context.onProgress(2); assert.equal(importer.status("import-normal")?.progress, 1);
    text.resolve("  fixture\n题面  "); assert.equal((await pending)?.problemText, "fixture\n题面");
    assert.equal(importer.status("import-normal")?.phase, "complete"); assert.equal(importer.cancel("import-normal").cancelled, false);
  });
  it("取消立即停止等待，迟到提取不能覆盖后续导入，取消可重复调用", async () => {
    const ready = deferred<ExtractionContext>(); const text = deferred<string>(); let calls = 0;
    const importer = new ProblemImporter({ ...options(), extract: async (_e, _b, context) => {
      if (++calls > 1) return "new result"; ready.resolve(context); return text.promise;
    } });
    const first = importer.import("import-first"); const context = await ready.promise;
    assert.equal(importer.cancel("import-first").cancelled, true); assert.equal(importer.cancel("import-first").cancelled, false);
    assert.equal(await first, null); assert.equal(context.signal.aborted, true); assert.equal(importer.status("import-first")?.phase, "cancelled");
    assert.equal((await importer.import("import-next"))?.problemText, "new result");
    text.resolve("late old text"); await Promise.resolve(); context.onProgress(.9);
    assert.equal(importer.status("import-next")?.phase, "complete"); assert.equal(importer.status("import-first"), null);
  });
  it("处理超时中止并释放锁；选择框时间不计入处理期限", async () => {
    const choice = deferred<string>(); let signal: AbortSignal | undefined;
    const importer = new ProblemImporter({ select: () => choice.promise, ocrCachePath: temp, timeoutMs: 20,
      extract: async (_e, _b, context) => { signal = context.signal; return new Promise(() => {}); } });
    const rejected = assert.rejects(importer.import("import-timeout"), /解析超时/);
    await new Promise(yes => setTimeout(yes, 35)); assert.equal(importer.status("import-timeout")?.phase, "selecting");
    choice.resolve(path); await rejected; assert.equal(signal?.aborted, true); assert.equal(importer.status("import-timeout")?.phase, "timed_out");
    for (const timeoutMs of [0, NaN, 600001]) assert.throws(() => new ProblemImporter({ ...options(), timeoutMs }), /时限/);
  });
  it("退出和销毁中止迟到选择；会话变更后仍能导入，销毁后不能再启动", async () => {
    const choice = deferred<string>(); const importer = new ProblemImporter({ select: () => choice.promise, ocrCachePath: temp, extract: async () => "fixture" });
    const pending = importer.import("import-select"); importer.cancelActive(); choice.resolve(path);
    assert.equal(await pending, null); assert.equal((await importer.import("import-again"))?.problemText, "fixture");
    importer.dispose(); await assert.rejects(importer.import("import-closed"), /已关闭/);
    for (const invalid of [null, "short", "../bad-path", "x".repeat(65)]) {
      assert.throws(() => importer.status(invalid), /标识/); assert.throws(() => importer.cancel(invalid), /标识/);
    }
  });
  it("真实解析进程超时后可立即重试，原进程已退出；子进程错误归一", async () => {
    const abort = new AbortController();
    const extracting = extractInProcess(".txt", Buffer.from("fixture"), temp, { signal: abort.signal, onProgress: () => {} });
    abort.abort(new Error("fixture process stopped")); await assert.rejects(extracting, /process stopped/);
    const importer = new ProblemImporter({ ...options(), timeoutMs: 1 });
    await assert.rejects(importer.import("import-native-timeout"), /超时/);
    const normal = new ProblemImporter(options()); assert.equal((await normal.import("import-native-ok"))?.problemText, "fixture text");
    const signal = new AbortController().signal;
    await assert.rejects(extractInProcess(".pdf", Buffer.from("broken pdf"), temp, { signal, onProgress: () => {} }), /解析失败/);
    await assert.rejects(extractInProcess(".txt", Buffer.alloc(60001, 65), temp, { signal, onProgress: () => {} }), /6 万字/);
    const aborted = new AbortController(); aborted.abort(new Error("fixture cancelled"));
    await assert.rejects(extractInProcess(".txt", Buffer.from("x"), temp, { signal: aborted.signal, onProgress: () => {} }), /cancelled/);
  });
  it("解析运行时缺失时给出归一错误，不泄露路径且不残留子进程", async () => {
    const executable = process.execPath;
    try {
      process.execPath = join(temp, "missing-runtime");
      await assert.rejects(extractInProcess(".txt", Buffer.from("fixture"), temp,
        { signal: new AbortController().signal, onProgress: () => {} }), error => {
        assert.ok(error instanceof Error); assert.match(error.message, /服务无法启动|服务无法通信/); assert.ok(!error.message.includes(temp)); return true;
      });
    } finally { process.execPath = executable; }
  });
});
