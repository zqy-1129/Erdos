"""阶段5：写作方案(writing_recipe)与图表方案(figure_recipe)。
自研通用方案，evidence 为通用框架/历史模式观察，不虚构历史数值；kind=suggestion。"""
from . import core

STAGES = ["analysis", "model", "algorithm", "results", "validation", "sensitivity", "discussion", "abstract"]

WRITING = [
    ["wr-opt-model", ["optimization"], "model", "优化/调度类：建立目标函数与约束",
     ["目标变量与指标", "决策变量定义", "约束来源"], "约束逐条对应题意，目标函数与指标单位一致"],
    ["wr-opt-algo", ["optimization"], "algorithm", "优化/调度类：说明求解算法",
     ["模型规模与结构", "所选算法"], "复杂度与可解释性；NP 类问题说明启发式/松弛的取舍"],
    ["wr-opt-results", ["optimization"], "results", "优化/调度类：报告调度方案与目标值",
     ["最优方案", "目标值", "方案对比"], "表格汇总多组数据结果，给出作业效率/净收益等单位化指标"],
    ["wr-opt-validation", ["optimization"], "validation", "优化/调度类：检验方案有效性",
     ["模型输出", "参照或边界"], "用相对偏差/上下界/多原则对比检验，不单凭一次运行"],
    ["wr-mech-model", ["mechanism"], "model", "机理建模：从物理/几何机理推导模型",
     ["对象与坐标系", "机理关系"], "明确符号与假设，变量定义与量纲一致，公式编号"],
    ["wr-stat-model", ["statistical_analysis"], "model", "参数估计/统计：假设与估计方法",
     ["样本与统计量", "噪声/分布假设"], "区分点估计与区间估计，说明显著性/拟合优度口径"],
    ["wr-pred-model", ["prediction"], "model", "预测类：说明预测模型与变量",
     ["特征/输入", "目标/输出"], "训练与验证数据划分，避免用结果变量泄漏预测"],
    ["wr-pred-validation", ["prediction"], "validation", "预测类：检验预测误差",
     ["预测值与真值"], "给出 RMSE/MAE/R² 等口径与分母，残差是否独立同分布"],
    ["wr-eval-model", ["evaluation"], "model", "评价类：建立指标体系",
     ["评价对象与维度", "指标定义"], "指标权重/标准化方法与依据，避免主观赋权无解释"],
    ["wr-assumptions", [], "assumptions", "通用：模型假设",
     ["题面已知条件"], "假设逐条可检验，说明放宽/收紧的影响"],
    ["wr-symbols", [], "symbols", "通用：符号说明",
     ["已定义变量与参数"], "符号与单位成表，跨问不一致需统一"],
    ["wr-sensitivity", [], "sensitivity", "通用：敏感性分析",
     ["关键参数取值范围"], "单项变化与组合变化分开，给出稳健性结论"],
]

FIGURES = [
    ["fig-mech", "schematic", "mechanism", "model", "机理/几何示意图", "对象坐标、对象定义、几何关系"],
    ["fig-flow", "flowchart", "optimization", "algorithm", "求解流程/算法流程图", "步骤节点、分支条件"],
    ["fig-opt-cmp", "bar/line", "optimization", "results", "多方案/多组数据结果对比", "方案名、目标值、单位"],
    ["fig-pred-vs", "line", "prediction", "results", "实测与预测对照", "真值序列、预测值序列"],
    ["fig-resid", "scatter", "prediction", "validation", "残差诊断", "预测值、残差"],
    ["fig-sens", "line", "unknown", "sensitivity", "参数灵敏度曲线", "参数值、目标值变化"],
    ["fig-eval-bar", "bar", "evaluation", "results", "评价指标对比条形图", "对象、指标值、单位"],
    ["fig-gantt", "gantt", "optimization", "results", "调度甘特图", "作业开始/结束时刻、资源"],
]

FIGURE_COLUMNS = {
 'fig-mech': [('object_id','identifier'),('coordinate','geometry with length unit')],
 'fig-flow': [('node_id','identifier'),('next_node_id','identifier'),('condition','branch condition')],
 'fig-opt-cmp': [('scenario','category'),('objective','numeric with unit')],
 'fig-pred-vs': [('time','ordered time unit'),('observed','numeric with unit'),('predicted','same unit as observed')],
 'fig-resid': [('predicted','numeric with unit'),('residual','observed minus predicted, same unit')],
 'fig-sens': [('parameter','numeric with unit'),('objective','numeric with unit'),('baseline','current-run reference')],
 'fig-eval-bar': [('object','category'),('indicator','numeric with declared normalization and unit')],
 'fig-gantt': [('job_id','identifier'),('resource','category'),('start','time unit'),('end','same time unit, end >= start')]
}


def _wrecipe(i, spec, version):
    rid, ptypes, stage, goal, prereqs, note = spec
    return {
        "recipe_id": rid,
        "competition_id": "cumcm",
        "version": version,
        "schema_version": 2,
        "problem_types": ptypes,
        "applicable_conditions": "任务特征匹配 problem_types 且处于 stage='{}'".format(stage),
        "stage": stage,
        "goal": goal,
        "prerequisite_results": prereqs,
        "steps": [
            {"purpose": "检查输入证据", "note": "从当前任务的结果账本读取依赖；没有计算结果时禁止写数值结论"},
            {"purpose": "建立论述次序", "note": goal + "；先说明目标与依据，再给公式或方法，再解释结果"},
            {"purpose": "接入可复现计算", "note": "每个结果记录 run_id、代码版本、数据 SHA256、参数、单位与输出文件；失败计算显式标记"},
            {"purpose": "验证与落段", "note": note + "；检查公式符号、量纲、引用及图表编号，与前后问题共享符号账本"}
        ],
        "variable_explanation_requirements": "所有变量首次出现即定义并注明单位",
        "length_basis": {"source": "self_authored_general", "verified": False, "note": "篇幅预算为目标届次参数，非官方规定"},
        "verification_approach": "结果可回查、单位一致、不混入未核验声明",
        "common_mistakes": ["用历史数值冒充新结果", "单位/量纲不一致", "公式无符号定义"],
        "missing_fallback": {"note": note + "；缺数据时显式留空，不假填"},
        "evidence": [{"type": "generic_framework", "note": "自研通用写作建议，非历史论文复制"}],
        "kind": "suggestion",
        "review_status": "self_authored_reviewed",
        "external_consumer_allowed": True,
        "result_value_source": "current_task_computation_only",
        "length_budget": {"mode": "client_task_budget", "historical_distribution": None, "not_official_requirement": True},
    }


def _frecipe(i, spec, version):
    rid, ftype, ptypes, stage, purpose, req = spec
    return {
        "recipe_id": rid,
        "competition_id": "cumcm",
        "version": version,
        "schema_version": 2,
        "figure_type": ftype,
        "purpose": purpose,
        "stage": stage,
        "required_inputs": [
            {"field": "dataset_ref", "type": "asset_ref", "description": req},
            {"field": "computation_run_id", "type": "string"},
            {"field": "columns", "type": "array", "description": "字段名、单位、维度、系列角色"}
        ],
        "required_column_roles": [{'role':role,'meaning_and_unit':meaning} for role,meaning in FIGURE_COLUMNS[rid]],
        "validation_rules": ['source is current_task','data SHA matches current result artifact','computation status is succeeded or reviewed','series dimensions match','units declared and consistent'],
        "optional_inputs": [],
        "generation_conditions": "所需结果字段齐备时才生成",
        "skip_conditions": "缺必需数据时跳过并记录 missing，不预填历史值",
        "axis_spec": {"x": "自变量", "y": "目标量"},
        "legend_spec": "系列名+单位",
        "caption_spec": "图序号-标题，置于图下方，注明单位与数据来源(current_task/historical_reference)",
        "style_ref": {"figure_style": "cumcm-figure-style"},
        "output_formats": ["png", "pdf", "svg"],
        "evidence": [{"type": "generic_framework", "note": "自研通用图表建议"}],
        "data_status": "current_task",
        "review_status": "self_authored_reviewed",
        "problem_types": [] if ptypes == "unknown" else [ptypes],
        "external_consumer_allowed": True,
        "checklist": ["所需列与单位完整", "数值来自本次计算", "样本或系列数量匹配", "坐标范围与图例可读", "矢量和位图输出均可核验"],
    }

def check_figure_inputs(recipe, supplied):
    """Metadata guard for EN consumers; never calculates or invents plotting values."""
    from . import contracts
    missing=[r['field'] for r in recipe['required_inputs'] if r['field'] not in supplied]
    if missing:return {'status':'missing','missing':missing,'action':'skip figure and request current computation inputs'}
    contracts.validate('asset_ref',supplied['dataset_ref'])
    if supplied.get('source')!='current_task' or supplied.get('computation_status') not in ('succeeded','reviewed'):
        raise ValueError('figure inputs must come from a completed current computation')
    if not isinstance(supplied['computation_run_id'],str) or not supplied['computation_run_id']:raise ValueError('missing run identity')
    columns=supplied['columns']
    if not isinstance(columns,list) or any(not isinstance(c,dict) or not all(isinstance(c.get(k),str) and c[k].strip() for k in ('role','name','unit')) for c in columns):
        raise ValueError('columns must declare field, role and unit; use explicit dimensionless/category units where appropriate')
    roles={c['role'] for c in columns}
    missing=[c['role'] for c in recipe['required_column_roles'] if c['role'] not in roles]
    if missing:return {'status':'missing','missing':missing,'action':'skip figure and request required column roles'}
    return {'status':'ready','missing':[],'not_numeric_validation':'EN must still verify dimensions, time ordering and actual data values against result_card'}


def main(args):
    out_dir = core.CONTENT_DIR / "normalized" / "cumcm_delivery" / args.run_id / "recipes"
    out_dir.mkdir(parents=True, exist_ok=True)
    version = 2
    wr = [_wrecipe(i, s, version) for i, s in enumerate(WRITING)]
    for stage, goal, inputs, note in [
        ('analysis','逐问分析目标、数据与约束',['题面','附件清单'],'先确定交付结果，再选择模型；分类结果允许多个题型'),
        ('results','解释本次真实计算结果',['结果账本','数据与代码散列'],'结果按问题编号组织；每个数值可追溯计算'),
        ('validation','验证数值、约束与稳定性',['计算结果','独立基准或边界'],'报告误差、可行性残差、失败场景；不得无依据宣称准确'),
        ('discussion','总结优缺点与适用边界',['各问结果','验证与敏感性证据'],'具体指出假设和数据限制，以及改进成本'),
        ('abstract','最后综合摘要',['所有分问结论','最终结果表','已完成验证'],'摘要最后写，控制在目标届次一页，包含具体方法与真实结果')]:
        wr.append(_wrecipe(0,['wr-general-'+stage,[],stage,goal,inputs,note],version))
    fr = [_frecipe(i, s, version) for i, s in enumerate(FIGURES)]
    core.write_json(out_dir / "writing_recipes.json", wr)
    core.write_json(out_dir / "figure_recipes.json", fr)
    coverage = {
        "writing_recipes": len(wr), "figure_recipes": len(fr),
        "task_feature_groups": sorted({t for s in wr for t in s['problem_types'] or ["通用"]}),
        "stages_covered": sorted({s['stage'] for s in wr}),
        "note": "自研通用方案已作源码审核；kind=suggestion，不是经人工标注的历史写法或官方事实",
    }
    core.write_json(out_dir / "recipe_coverage.json", coverage)
    print("recipes 完成：写作方案={}，图表方案={}，任务特征组={}，阶段={}".format(
        len(wr), len(fr), len(coverage["task_feature_groups"]), len(coverage["stages_covered"])))
    return 0
