> 当前本地团队 v3 接收约定见 [LOCAL_V3.md](LOCAL_V3.md)；本页旧版本说明保留供追溯。

> 此文件为旧 Trae 交付说明。运行版本、地址与命令请使用 [Codex 本地交付对接手册](CODEX_LOCAL_HANDOFF.md)，旧命令不作为本次验收证据。

# 对接交付：引擎（EN）

状态：`existing`=DE 数据侧；`proposed`=引擎接入接口。

## 1. 检索请求（retrieve-request）

见 `handoff/cumcm/examples/retrieve-request.json`（A1 字段：competition/year_range/release/subproblem{goal,constraints,deliverables,problem_types,method_tags,data_tags,domain_tags}/stage/language/top_k/token_budget/usage_purpose）。

- `stage` 允许 analysis/assumptions/symbols/model/algorithm/results/validation/sensitivity/discussion/abstract；非法被拒 `invalid_request`。
- `usage_purpose=third_party_model` 且无许可 → `blocked_usage`（历史论文 send_to_third_party_model=false）。

## 2. 返回（context_capsule）

`retrieve` 返回：request_id/result/release_id/retrieval_mode(=lexical_only)/embedding_profile_id(=null)/stage/query_summary/units/writing_recipes/figure_recipes/missing/budget_actual/resource_locks。
- 历史 `units` 当前恒为空（license 未授权），`missing` 说明原因，仅返回自研 `writing_recipes`/`figure_recipes`。
- recipe 有完整字段：适用条件、stage、前置结果字段、长度口径（`verified=false`）、回退、证据（kind=suggestion）。

## 3. 结果登记合同（引擎→数据侧）

- `result_card.schema.json`：task/subproblem/input_refs/method/outputs/metrics/status/limitations/verdeps。
- `document_node.schema.json`：heading/paragraph/equation/figure/table/reference/appendix，subproblem_ref/result_refs 稳定逻辑 ID。
- 新题内容属调用输入，不写回历史库；结果为新计算，不复用历史数值。

## 4. 示例数据合同（fixture）

固定真实计算用于校验 recipe/result_card/document_node 兼容性（如 `scripts/run_demo.py` 的线性拟合+凸优化），不声称 LLM 通用解题。
