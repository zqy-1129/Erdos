"""引擎进程的宿主环境变量消毒（隐私红线防御）。

`langgraph` 传递引入 `langchain-core`/`langsmith`，其追踪开关由环境变量决定：
宿主只要设了 `LANGCHAIN_TRACING_V2=true`（客户端 `main/engine-host/host.ts` 用
`{...process.env}` 原样继承），每次图执行都会把节点输入/输出 POST 到 LangSmith 端点，
而阶段产出含题面片段与论文正文 —— 违反「题面不出本机」(DEC-010) 与 BYOK 不中转。

调用时机是硬约束：`langsmith.utils.get_env_var` 带 `@lru_cache`，首次读取后按名缓存，
之后再改 os.environ 无效。因此剥离必须发生在任何 engine 子模块（及其 lang* 依赖）
被导入之前 —— 即 `engine/__init__.py` 内，而不是 `main()` 里。
"""

import os

FRAMEWORK_ENV_PREFIXES = ("LANGCHAIN", "LANGSMITH")


def sanitize_framework_env(environ: dict[str, str] | None = None) -> list[str]:
    """移除所有 lang* 框架的 env 开关，返回被移除的变量名（供日志与测试断言）。"""
    env = os.environ if environ is None else environ
    removed = [k for k in list(env) if k.startswith(FRAMEWORK_ENV_PREFIXES)]
    for key in removed:
        del env[key]
    return removed
