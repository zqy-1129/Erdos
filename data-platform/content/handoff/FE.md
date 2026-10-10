# 客户端接收规范

用户先选择 `competition_id`，再选 latex/docx。读取目录的 family_version、ruleset_status、consumable_status；当前全部 draft/规则未核验，不能显示“官方格式已确认”。

```powershell
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/local_content_consumer.py list
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/local_content_consumer.py select cumcm --format docx
```

select 的 verified_resources 包含真实读取校验后的 asset_ref 和包内相对位置。客户端下载应使用可信下载描述提供的 ID/version/hash/size，而不是开发机 D 盘路径。无需一次下载原件全集，先取得启动用 ruleset、布局、基础框架、写作策略、图表样式；历史内容画像在允许时按需检索。

`get <asset_id> --version <version> --output <本地文件>` 输出的 JSON 是校验描述，文件字节写到 output；不加 output 时仅缓存，不能把 stdout 当文件内容。缓存键绑定 ID/version/hash。缓存不符时报错保留现场，不返回 verified，也不自动覆盖旧版本。

LaTeX 资源是完整 ZIP。解包前拒绝绝对路径、..、反斜杠、重复成员、Windows 特殊/尾点尾空格路径、链接、加密和超限文件，校验字节后在受控目录解包。DOCX 是真实 OOXML 文件，不能用 JSON 改扩展名充当 Word。

outline 接受新题 task-json 或显式 fixture 小问数，生成按小问成组的章节和 current_task 图表要求。task_context 是本次资源锁，不是论文结果；verified 只表示文件字节校验，rules_compliance 仍是 unverified。预算没给则为 null；用户给了预算也不是官方页数保证。

无匹配：`result=no_reference`，正常显示通用框架；不能把未授权样本或其他赛事答案填进来。规则待核验、待人工审核、无参考、下载失败分别呈现给用户，不复用一个“完成”状态。

建议的 IPC/HTTP 流程是：目录→启动描述→校验下载→新题分析→画像检索→框架和当前计算结果→文稿预览。实际产品 IPC 和后端鉴权接口尚未实现；DE 的 CLI 可以直接用于联调验证。
