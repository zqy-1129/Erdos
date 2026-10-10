# Agent / 引擎接收规范

数据层提供格式 family 与有证据的历史内容画像，求解与成文由引擎使用当前任务输入完成。历史论文的结论、数值和图像不能作为新题结果。

## 新题输入和检索

把用户新题与数据作为独立 task 保存；模型输出每个小问的 problem_types/method_tags/data_tags/domain_tags、置信与依据。题型可多选，不根据赛事名指定算法。LLM API 的模型名称、密钥、鉴权和调用尚未实现；本地 demo 分类明确为 deterministic_fixture。

真实 task-json 需要 task_id、competition_id、subproblems（每项含唯一 subproblem_id 与标签），is_fixture=false。题型标签取 taxonomy_v1；证据与原文定位由引擎保留。当前 outline 读取此输入，不会自动分析题意。

```powershell
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/local_content_consumer.py search cumcm --tags 'optimization,prediction'
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/local_content_consumer.py search cumcm --tags 'optimization,prediction' --tags-all
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/local_content_consumer.py outline cumcm --task-json out/tasks/new-task.json --format latex --budget 12000
```

默认 OR 匹配不同标签；`--tags-all` 是 AND。排名按不同匹配标签数降序，然后 case_id 升序，不伪装成向量相似度。成功返回 profile_asset_ref（ID/version/hash）、父题和源 hash；画像 JSON 自身的字节也校验。无审核或授权样本返回 no_reference 和 generic_framework。当前真实库因此不会返回可供模型使用的历史案例。

## 写作与图表

outline 按每个小问连续展开 analysis/model/solve/validation，不固定三问；格式、ruleset、写作策略、图表样式都从当前 family 取得。参考画像的 structure/writing/length/figures 与 source_sha256/evidence 绑定。原文篇幅尚不能可靠计算时为 null；可读取旁路 recognized_text_metrics，不能当作正文长度。自动图题候选不能当作已确认图型。

求解输入、算法版本、数据 hash、结果 hash 进入 trace。每张图来自当前变量、单位、计算结果，绘图样式可参考已许可的历史案例。图型建议依据本次题型，仍需当前结果支持；图表公式、回归指标、目标函数最优值需要实际计算验证。

## 模型权限

internal_analysis、distribute_original、distribute_profile、send_to_third_party_model 独立判断。本地审核 `get --purpose audit` 不允许发往模型。`--model-execution third_party` 会执行资源与案例双重门禁；许可为 false 时不能绕过。即使检索得到画像，发送第三方也须另查该用途权限。

本地 demo 从指定 release 启动、重新读取 CSV 做 OLS，读取独立 objective.json 做网格优化，与解析解对照，绘制拟合+残差与目标曲线，使用实际 family 样式/布局及动态 outline 填充 Word/LaTeX，并保存 task_context/trace/integrity。它证明本地数据流与计算可执行，不证明通用 Agent 或外部 LLM 已接通，也不是完整可提交论文。
