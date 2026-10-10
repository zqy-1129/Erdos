> 2026-10-05 最终审查修正：figure_style 的内容 version 改为正整数（原先错误引用 const=1 的 schema_version）；schema_version 仍固定 1，其他 16 个 JSON 契约不变。当前使用入口是 [handoff](../../handoff/README.md) 和 [数据平台 README](../../README.md)。新增内容版本不需要升 schema_version。

# Erdos 数据契约 v1：数据工程师对接说明

状态：**DE 内部 draft-ready**。本阶段定义可校验数据规范；BE/FE/EN 的现有接口没有改动，跨角色契约仍需项目评审。最新验收见 `D:/Erdos/reports/trae/stage02_codex_acceptance.md`。

## 1. 从用户选择比赛到调用数据

```text
competition_registry：19 个比赛入口
  → template_family：所选赛事的格式 + 内容资源
      → layout_refs：LaTeX / Word，按需选择
      → base_outline：摘要、建模等章节；子问题章节可重复组合
      → writing_policy：基础写作策略
      → figure_style / table_style：画法、排版样式
      → ruleset：届次、轮次、赛道、来源、核验状态
  → problem_profile：历史题目与各子问题的题型/方法/数据/领域标签
      → case_profile：关联论文的结构、写法、篇幅和图表出处
          → writing_profile：同赛事/题型的多篇统计与小样本回退
  → asset：实际文件的 ID/version/hash、对象键与许可范围
  → release_manifest：发布资源及依赖、增量和下架
  → task_context：FE 校验本地资源后，交给 EN 的版本锁定载荷
```

新题由 EN 分析各子问题，检索当前赛事内的候选参考；真实求解结果对应 `figure_spec.data_ref`，图表风格对应 `style_ref`。历史论文的图表只属于 `historical_reference`，不能标为新题结果。FE 调 BE 获取清单和下载资源；EN 使用 FE 交付的本地资源，不直接扫描数据工程师的 D 盘。

## 2. ID、版本、路径

- 19 个 competition_id 与注册表一致；共享 `00_通用模板` 不占第 20 个赛事。
- ID 表示稳定身份；实体版本独立递增。problem/case 使用 profile_version，family 使用 family_version，资源/样式/写作画像使用 version。schema_version=1 表示本规范结构版本，其他结构版本应先迁移，不能直接接受。
- release_id 标识发布实例；source_sha256 表示原件字节，不代替画像文件本身的 sha256。
- 所有资源引用均采用 `{asset_id, version, sha256}`。画像引用另携带 case_id/profile_version/source_sha256，FE 交付还须锁定画像 asset 并声明已缓存校验。
- 每个 bundle 是一个选定版本的校验快照，同类稳定 ID 在快照中唯一。历史快照分别保存，不能覆盖旧版。跨历史快照的多版本存储、解析和升级属于阶段六发布构建实现，不把本阶段单快照校验器当成历史版本仓库。
- 原件仍放 `D:/Erdos_data`，只读。业务相对路径使用 `/`；拒绝绝对路径、盘符、URI、`..`、反斜杠及控制字符。真实源文件名允许字面 `%`；对象键另拒绝 `%/?/#` 中的百分号、查询和片段字符（路径分隔 `/` 合法）。签名 URL 不入永久索引。
- `asset.oss_key` 是计划对象键，本地校验通过不代表已经上传。`task_context.local_resources.relative_path` 相对 FE 缓存根；不把开发目录冒充云端地址。

## 3. 分类与出处

六类工作题型：optimization/prediction/evaluation/mechanism/classification/statistical_analysis，另有 unknown。它们是检索口径，不是赛事官方分类。

problem_types、method_tags、data_tags、domain_tags 独立；各子问题可多标签。unknown 不能与确定题型共存；unknown 置信度为 null；approved 标注须有置信度和明确来源。整题类型应包含已识别的子问题类型。回归集类别不能直接作金标准。

方法使用稳定英文 code，`taxonomy_v1.method_tags.entries` 保存中文 legacy_tag 和 aliases；当前映射覆盖方法词典的全部 105 个标签。旧 CaseRecord 中的中文词不改写，由适配层显式转换。

case_profile 保存原章节名、规范 section_type、层级、子问题关系、页码；写作观察区分 fact/suggestion。事实必须携带原件 hash + 页码或文本区间的证据。篇幅分别记录 zh_chars/en_words，并各自拆正文、摘要、目录、附录及统计依据；缺失用 null，不把估算冒充精确统计。历史图表的类型、用途、变量、图注组织和证据与新任务图表规格分开。

writing_profile.sources 锁定每篇参考的 profile_version/source_sha256；sample_count 必须等于不同 case_id 数量。统计明确单位，分位数有序，零样本须有回退，不按固定 12 篇凑数。

## 4. 授权、审核、规则核验

四种许可独立：internal_analysis、distribute_original、distribute_profile、send_to_third_party_model。未知许可全部 false；肯定许可须有 license_evidence。合规提示和审核通过都不能自动产生授权。

- 内部分析许可不等于分发许可，也不等于发送第三方模型许可。
- published 发布逐项检查实际许可范围及 license_status；画像和汇总写作策略还检查关联原论文的许可。发布模板须有完整资源及审核状态。
- 未解析父题使用 problem_id=null/resolution_status=unresolved，仅可 staging；确定父题必须存在且同赛事，不能捏造题号。
- `model_execution=local_only|third_party` 明确消费方式；第三方方式须逐一检查本地资源及参考画像的模型发送许可。
- ruleset 区分 official/historical_experience，verified 须有届次、来源及核验时间。未核验规则不能令 task_context.rules_compliance=verified。

这些是数据字段及调用约束；真实许可与当届规则结论仍由负责人依据证据确认。

## 5. Schema 与校验层次

共 17 个 schema 文件（含共享定义）：competition_registry、source_file、taxonomy、problem_profile、case_profile、template_family、asset、release_manifest、writing_profile、figure_style、figure_spec、ruleset、base_outline、table_style、task_context、bundle 及 `_common`。

实体使用严格键；关键嵌套字段也有类型和边界。新增非关键说明可放 extensions；不能覆盖版本、许可、引用等定义字段。可选字段演进与新增必填字段失效由真实 asset schema 回归覆盖。注册表和 taxonomy 保留其既有说明字段，不把描述性扩展作为业务约束。

JSON Schema 检查形状、类型、枚举、hash、路径；bundle 校验还检查唯一性、外键、赛事、所有标签字典、版本/hash、来源证据、许可、规则状态、发布依赖循环、增量继承/下架和模板资源闭环。错误含规则 code 和记录/字段位置。

单实体 schema 通过不代表跨文件通过，交付真实关联数据必须运行 `--check-bundle`。JSONL 流式读取并报行号；重复路径、无记录、非对象、NaN/Infinity、重复 JSON 键和不可读输入会失败。所有 `$ref` 在本地解析；缺失或远程引用在载入时失败。

date-time 显式检查日历、时区及时间字段，本产品时间戳使用普通民用秒（00–59），不使用闰秒时间戳。

## 6. 可复现命令与依赖

只用用户指定的 Python 3.8，不涉及 MAMBA。工作目录 `D:/Erdos/data-platform/content`：

```powershell
$py = 'E:\Anaconda\envs\pytorch\python.exe'
# 离线恢复及逐文件 hash 核验；不安装到 E 盘环境
& $py -S scripts/bootstrap_schema_validation.py
& $py -S scripts/bootstrap_schema_validation.py --verify-only
# schema、真实注册/清单及所有正负例
& $py -S scripts/validate_content_contracts.py --all --json-report out/inventory/content_contracts_report.json
# 实际跨记录数据包（需包含所引用的实体和资源）
& $py -S scripts/validate_content_contracts.py --check-bundle examples/contracts/v1/positive/bundle_ok.json
& $py -S -m unittest discover -s tests -p 'test_*.py' -v
```

退出码：0 通过，1 数据/规范/输入失败，2 工作区依赖缺失。`-S` 验证不借用环境第三方库。依赖锁为 jsonschema 4.16.0、pyrsistent 0.18.0、attrs 21.4.0、importlib-resources 6.4.0、zipp 3.20.2；完整版本与 184 个文件 hash 见 dependencies.lock.json。pyrsistent 使用纯 Python 实现，避免借用环境 C 扩展。

初始指令建议 4.17.3，Trae 未能联网下载；本次将实际可用的 4.16.0 锁定为内部验收基线，补齐缺失模块并通过隔离环境测试。版本变化已记录，不再把未安装版本列为已使用。

离线恢复依赖当前本机 conda 缓存及已安装 Python 包的只读副本，来源目录逐项记录在锁文件；来源丢失时报失败，不联网回退。其他机器可按 requirements-schema-validation.txt 安装进 `.deps/schema_validation`，再核验运行；文件 hash 是本机缓存副本的复现依据，重新安装的元数据字节可能不同，应审核后重建机器锁文件。

## 7. 既有记录的逐字段映射

旧 `out/pack` 保持原样，新结构为独立配套记录。

| 既有实体/字段 | v1 映射 |
|---|---|
| ProblemRecord.business_id | problem_profile.problem_id，相等 |
| competition | competition_id，相等 |
| year、problem_code | 配套可冗余，旧记录为基础值来源 |
| title、prompt_zh、prompt_en | 仍在 ProblemRecord，丰富抽取文本另存资源，不能用画像取代题干 |
| attachments | 旧数组不改；新 asset(role=attachment, owner_id=problem_id) 与来源记录关联 |
| scoring、dataset_hint、visibility、tags | 保留原记录；tags 不直接当新题型金标准 |
| TemplateRecord.business_id | 显式映射为 layout asset_id；family_id 与业务 ID 分开 |
| competition、format | family.competition_id、layout_refs.format 与 asset.format |
| oss_key、sha256、version | asset.oss_key/sha256/version；family_version 独立 |
| changelog、tier | 保留 TemplateRecord；不在画像中重定义计费 |
| CaseRecord.business_id、problem_id | case_profile.case_id、problem_id |
| title、award、compliance_note | 保留原记录；奖项须真实证据，提示不产生许可 |
| method_tags | 旧中文标签保持不变，经 taxonomy 映射到配套稳定 code |
| oss_key、sha256 | 源 asset(case_paper) 对应；画像 source_sha256 相等 |

仍为十二键 ProblemRecord、八键 TemplateRecord、八键 CaseRecord，不能将本目录扩展字段直接塞入旧导入包。

## 8. 各角色交付边界

| 角色 | 本阶段可交付及后续确认 |
|---|---|
| DE | 注册、标签字典、标准化资源、配套画像、来源证据、清单和校验报告；阶段三先做真实小样本 |
| BE | 评审新增数据表/版本索引/许可字段/下载接口；既有 contracts、Alembic 均未改 |
| FE | 评审目录缓存、按需下载、字节 hash 检查、安全解包、task_context 与 IPC；verified 声明须由真实校验产生 |
| EN | 分析新题子问题，按赛事/题型/方法检索，结合求解结果写新论文及新图表；模型发送范围须受资源许可控制 |
| ARCH/负责人 | 冻结跨角色字段与消费方式，裁决真实许可、当届规则与 tier；不是 DE 单方面冻结接口 |

建议接口和表详见原存储设计文档；本阶段不创建它们。阶段三可使用 DE 内部规范做样本，不把跨角色接口冻结当作整理只读资料的前置门槛。

## 9. 示例与实际成果的区别

全部示例标 is_fixture，hash/大小及规则/许可决定为夹具数据；用于验证契约，不是实际源文件核验、奖项、官方格式或授权证明。正例涵盖一题多论文、混合子问题、两种格式、未知/未授权、未解析父题、许可发布和增量下架；反例实际运行校验并要求命中指定规则。

本阶段未建成十九套正式模板，未抽取全部优秀论文，未实际上传 OSS，未执行产品端到端。后续数据生产须将原件重新流式计算 hash，将这些夹具替换为真实可追溯记录，并验证提取文本、图表数据及文档渲染。
