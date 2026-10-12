/** 隔离解析进程：只接收文件字节与 OCR 缓存目录；退出时所有内部 OCR 线程随进程回收。 */
import { extractProblemText } from "./problem-extraction.ts";

process.once("message", async (input: { extension: string; bytes: Uint8Array; ocrCachePath: string }) => {
  try {
    const text = await extractProblemText(input.extension, Buffer.from(input.bytes), input.ocrCachePath,
      progress => process.send?.({ kind: "progress", progress }));
    process.send?.({ kind: "result", text }, () => process.disconnect?.());
  } catch (error) {
    // 第三方异常可能带路径或文件内容，只有本地资源限制文案透出。
    const message = error instanceof Error && /^(Word|PDF|图片|不支持)/.test(error.message)
      ? error.message : "题面解析失败，请检查文件或改为手动粘贴";
    process.send?.({ kind: "error", message }, () => process.disconnect?.());
  }
});
