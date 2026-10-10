# 服务端接收规范

候选包 `out/releases/content-candidate-codex-v3` 是完整、本地、自足快照；未上传 OSS、未运行迁移或真实 content_seed 入库。可先本地联调：

```powershell
& 'E:/Anaconda/envs/pytorch/python.exe' scripts/run_content_pipeline.py check
```

## 包内容与旧字段

根目录 `catalog.json` 列出 19 个赛事；`integrity.json` 覆盖元数据与资源字节。每个 `competitions/<cid>` 包含 bundle、manifest、assets、local_bindings、index、seed、files。`asset.oss_key` 是原始便携逻辑路径；包内真实位置必须通过 `local_bindings[asset_id]` 取得，不能把两者混用。

旧 seed 的字段集合严格兼容 `server/app/domain/entitlement/ports.py`：

| 记录 | 键集合 |
|---|---|
| ProblemRecord（12 键） | business_id, competition, year, problem_code, title, tags, prompt_zh, prompt_en, attachments, scoring, dataset_hint, visibility |
| CaseRecord（8 键） | business_id, problem_id, title, award, method_tags, oss_key, sha256, compliance_note |
| TemplateRecord（8 键） | business_id, competition, format, oss_key, sha256, version, changelog, tier |

源哈希等新增字段放在 `seed/provenance.json` 与配套 bundle，不能塞进旧 seed。新确认的题目/论文尚无旧格式 seed 的，先以 profile 接入新表，不虚构奖项、评分或原题文案。旧 seed 仍含既有历史描述，不意味着这些案例已获重新审核或发布授权。template tier=free 是候选包适配占位，不是产品定价决策。


当前旧格式 seed 仅提供 10 题、7 篇已确认且父题 seed 存在的论文、38 个格式条目；其余候选不会以未确认旧父题入库。全部 240 个旧案例身份仍保留于新画像/来源映射。

## 新表和导入顺序（建议）

建议增加 asset、problem_profile、case_profile、template_family、ruleset、outline、style、writing_profile 和 release 索引；定义见 schemas/v1。所有实体按稳定业务 ID + 内容版本存储；资源按 asset_id + version + sha256 锁定。

采用事务或暂存表：先写无引用的主体身份，再写资产与组件，最后闭合引用；延迟检查外键，整体通过后激活快照。若先插资产而 owner 外键立即生效，会失败。不要把新增字段塞进现有表接口。

相同 ID/版本/hash 幂等；相同 ID/版本但 hash 不同拒绝写入，要求新版本。该候选包 manifest 是独立完整快照（所有 items=add、dependencies=[]），不能当作增量差异。删除/下架根据审核后的快照集合或另行生成受校验的 delta；切换后旧资源保留但停止向新任务发放。

## 下载与发布门禁（建议）

OSS 对象键建议：`content/<cid>/assets/<asset_id>/v<version>/<sha256>/<filename>`。签名 URL、对象 key 与本地绑定分别存储；不要覆盖同一路径的旧内容。下载描述至少含 ID/version/hash/size/media_type、用途、授权、状态和有效期；hash 从可信服务端获得，不能仅信任同来源的可编辑清单。

当前历史论文四项授权均为 false，审核 pending，父题也可能 unresolved，不能上线发放、分发原文或发送第三方模型。通用草稿本地可检查；对外发布仍需逐资源确认权限。激活前必须有正式授权记录、对应年份规则证据、通过语义校验和资源字节校验。

BE 应提供目录/启动描述、检索、资源下载、发布状态接口；这些 HTTP 路由尚未由 DE 实现。已有本地 select/search/get/outline 可作为输入输出样例。
