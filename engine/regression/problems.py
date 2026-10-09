"""真题回归集（SP1-7 验收方案 2026-10-06 口径）：≥20 道，覆盖 4 类题型。

对齐《SP1-7 MVP 集成验收方案》§2：回归集 ≥20 道分层抽样，覆盖优化/评价/预测/机理
4 类题型；每题含题面、参考答案要点锚点（QA 冻结前校对）。旧 10 题口径（10-01）已废弃。

冻结纪律：11-05 冻结后不再改样本；manifest() 输出逐题内容哈希与集合级 set_sha256，
作为冻结清单校验依据（判定报告附录引用）。

题面说明：条目为 EN 侧整理的简化改写（非竞赛原文，规避版权分发）；QA 校对与
内容库 problems 域入库（题面/参考答案/评分 rubric）为共同建任务的一部分。
"""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

CATEGORY_OPTIMIZATION = "optimization"
CATEGORY_EVALUATION = "evaluation"
CATEGORY_PREDICTION = "prediction"
CATEGORY_MECHANISM = "mechanism"
CATEGORIES = (CATEGORY_OPTIMIZATION, CATEGORY_EVALUATION, CATEGORY_PREDICTION, CATEGORY_MECHANISM)

DIFFICULTY_BASIC = "basic"
DIFFICULTY_ADVANCED = "advanced"


@dataclass(frozen=True, slots=True)
class RegressionProblem:
    """一道回归题：题面 + 参考要点 + 分层元数据。"""

    business_id: str
    category: str  # optimization / evaluation / prediction / mechanism
    title: str
    statement: str  # 题面全文（简化改写；冻结前 QA 校对）
    reference_digest: str  # 参考答案要点锚点（模型族 + 关键检验点）
    year: int
    difficulty: str = DIFFICULTY_BASIC  # basic / advanced

    def to_seed_record(self) -> dict[str, str]:
        """内容库 problems 域入库记录（题面/参考要点/分类；rubric 走 gates 版本化体系）。"""
        return {
            "business_id": self.business_id,
            "category": self.category,
            "title": self.title,
            "statement": self.statement,
            "reference_digest": self.reference_digest,
            "year": str(self.year),
            "difficulty": self.difficulty,
        }


# 20 道回归题（分层：优化 7 / 评价 4 / 预测 4 / 机理 5；来源 CUMCM 2012~2024）
REGRESSION_SET: tuple[RegressionProblem, ...] = (
    # ---- 优化类（7 道）----
    RegressionProblem(
        "cumcm-2018-B", CATEGORY_OPTIMIZATION, "智能RGV的动态调度策略",
        "一条智能加工生产线由 8 台 CNC 机床、1 处清洗工序与 1 台 RGV 直线导轨车组成。"
        "RGV 沿导轨往返为 CNC 传送物料，CNC 按上料、加工、清洗循环作业，不同工位耗时与"
        "移动时间已知。针对单工序加工、双工序加工、双工序且 CNC 可故障（故障修复时间随机）"
        "三种工况，分别给出一个班次（8 小时）内 RGV 的动态调度策略，使成料产量最大。",
        "调度模型族：事件驱动仿真 / 整数规划 / 启发式规则。关键检验点：CNC 状态机与"
        "RGV 移动时间矩阵正确建模；故障工况的鲁棒调度；班次内成料计数可复算。",
        2018, DIFFICULTY_ADVANCED,
    ),
    RegressionProblem(
        "cumcm-2020-B", CATEGORY_OPTIMIZATION, "穿越沙漠游戏策略",
        "玩家从起点出发，须在限定天数内穿越沙漠到达终点。地图含村庄、矿山与不同天气的"
        "关卡：晴朗/高温/沙暴天气下水和食物消耗不同；可在起点与村庄购买水与食物（价格"
        "随天气浮动），在矿山挖矿获得收益但消耗补给。给出不同起始资金下的最优穿越与"
        "挖矿方案，使到达终点时剩余资金最多。",
        "模型族：动态规划 / 状态空间搜索。关键检验点：水食物存量与天气消耗耦合；"
        "挖矿收益与补给消耗的权衡；终点剩余资金可复算。",
        2020,
    ),
    RegressionProblem(
        "cumcm-2021-A", CATEGORY_OPTIMIZATION, "FAST 主动反射面优化",
        "500 米口径球面射电望远镜（FAST）反射面为主索网结构，通过下拉索控制索网节点"
        "在基准球面与工作抛物面间变位形成照明区域。给定观测方向与照明圆盘约束，建立"
        "节点径向调整模型：确定工作抛物面参数与各节点径向位移，使照明区域内反射面"
        "对照明圆盘的拟合精度最高，并给出最优解的精度指标。",
        "模型族：曲面拟合 + 非线性优化（最小二乘）。关键检验点：抛物面参数（顶点/"
        "焦距）与照明圆盘投影几何；节点径向位移约束；拟合误差量化指标。",
        2021, DIFFICULTY_ADVANCED,
    ),
    RegressionProblem(
        "cumcm-2022-B", CATEGORY_OPTIMIZATION, "无人机编队纯方位无源定位",
        "若干架无人机编号 FY00~FY09 组成圆周编队，其中少量无人机发射信号、其余通过"
        "接收信号的到达方向角（无距离信息）确定自身位置并调整至均匀圆周分布。建立"
        "纯方位无源定位模型：由方位角解算未知无人机位置，并给出逐步调整至标准圆形"
        "编队的方案与误差分析。",
        "模型族：非线性最小二乘 / 多站纯方位定位。关键检验点：方位角测量方程组；"
        "位置解算的唯一性与误差传递；逐架调整收敛性。",
        2022, DIFFICULTY_ADVANCED,
    ),
    RegressionProblem(
        "cumcm-2023-A", CATEGORY_OPTIMIZATION, "定日镜场的优化设计",
        "圆形定日镜场通过定日镜将太阳光反射至吸收塔集热器。给定塔高与镜场尺寸范围，"
        "定日镜尺寸、安装高度与位置布局待定。建立镜面反射光学效率模型（含余弦效率、"
        "阴影遮挡、大气衰减），在额定输出热功率约束下，以单位镜面面积年平均发电量"
        "最大为目标优化整个镜场设计参数。",
        "模型族：几何光学建模 + 启发式布局优化。关键检验点：余弦效率与阴影遮挡"
        "计算；镜场网格布局离散化；约束优化收敛与年发电量复算。",
        2023, DIFFICULTY_ADVANCED,
    ),
    RegressionProblem(
        "cumcm-2023-B", CATEGORY_OPTIMIZATION, "多波束测线布设",
        "多波束测深船以固定开角向海底发射声波，覆盖宽度随水深与坡面倾角变化。对给定"
        "矩形待测海域（含坡面几何），建立覆盖宽度与重叠率模型：设计一组测线位置使"
        "海底全覆盖、相邻测线重叠率控制在 10%~20%，并使测线总长度最短；给出不同"
        "坡度情形下的测线方案与覆盖效率。",
        "模型族：几何覆盖模型 + 间距优化。关键检验点：覆盖宽度-深度/坡度关系；"
        "重叠率约束的边界验证；测线总长最短性。",
        2023,
    ),
    RegressionProblem(
        "cumcm-2024-B", CATEGORY_OPTIMIZATION, "生产过程中的决策",
        "企业生产某产品需经多道工序，零配件与成品存在次品率；对零配件和成品可抽样"
        "检测（存在漏检/误检），不合格成品可拆解回收（拆解有费用）。在给定的多组"
        "工序情形（次品率、检测与拆解费用不同）下，分别为各工序决策是否检测零配件/"
        "成品、是否拆解不合格品，使单位产品总成本最低或利润最大。",
        "模型族：决策树 / 期望值优化。关键检验点：抽检方案的漏检误检概率传递；"
        "拆解回收费用权衡；多工序串联不合格率合成。",
        2024,
    ),
    # ---- 评价类（4 道）----
    RegressionProblem(
        "cumcm-2018-C", CATEGORY_EVALUATION, "大型百货商场会员画像描绘",
        "大型百货商场提供会员个人档案、消费流水与商品信息数据。构建会员画像模型："
        "对会员按购买力、消费偏好、活跃度等维度量化分层，识别高价值会员、流失风险"
        "会员与潜在激活会员，给出各类会员的判定标准与针对性营销策略建议。",
        "模型族：RFM / 聚类 + 综合评价。关键检验点：画像指标体系构建与权重确定；"
        "会员分层的可解释性；策略与画像挂钩。",
        2018,
    ),
    RegressionProblem(
        "cumcm-2019-C", CATEGORY_EVALUATION, "机场卫星厅对中转旅客满意度影响",
        "机场拟新建卫星厅以扩大容量，卫星厅与主航站楼间以捷运连接。中转旅客的衔接"
        "流程（下机-中转-登机）时间分布将随之改变。建立中转流程与航班衔接模型，"
        "量化评价不同卫星厅规模与运行方案对中转旅客最小衔接时间、衔接成功率与"
        "舒适度（满意度）的影响，并给出方案比选结论。",
        "模型族：衔接概率 / 排队模型 + 满意度综合评价。关键检验点：中转时间分布"
        "建模；航班对衔接成功率计算；方案间量化比选。",
        2019,
    ),
    RegressionProblem(
        "cumcm-2020-C", CATEGORY_EVALUATION, "中小微企业信贷决策",
        "银行依据中小微企业的实力与信誉（进货/销项发票等经营数据与违约记录）决定"
        "是否放贷及贷款额度、利率与期限。建立企业实力与风险量化评价模型，对附件中"
        "企业进行信用评级；在年利率 4%~15%、额度 10 万~100 万约束下给出 125 家企业"
        "的信贷策略，使银行收益最大且违约风险可控。",
        "模型族：评分卡 / 综合评价 + 决策优化。关键检验点：指标体系与权重；违约"
        "风险量化；利率-额度联合决策。",
        2020,
    ),
    RegressionProblem(
        "cumcm-2022-C", CATEGORY_EVALUATION, "古代玻璃制品成分分析与鉴别",
        "古代玻璃易风化，风化前后化学成分比例变化明显。给定一批玻璃文物的成分检测"
        "数据（含风化信息与类型标注：高钾/铅钡），分析成分统计规律与风化的关系；"
        "建立类型鉴别模型对未知样品分类，并对风化文物预测其风化前的成分含量。",
        "模型族：统计检验 + 分类模型。关键检验点：风化前后成分差异显著性；分类"
        "规则的准确率验证；成分回补预测合理性。",
        2022,
    ),
    # ---- 预测类（4 道）----
    RegressionProblem(
        "cumcm-2012-C", CATEGORY_PREDICTION, "脑卒中发病环境因素分析及干预",
        "提供某地区多年脑卒中发病人数记录与同期气象（气温、气压、相对湿度等）数据。"
        "分析发病人数与气象因素的关系（含滞后效应），按高风险/低风险时段建立发病"
        "预测模型；据此给出高危时段预警与干预建议，并评估预测精度。",
        "模型族：时间序列 + 相关/回归分析。关键检验点：气象滞后效应处理；分人群"
        "或分时段建模；预测精度与验证口径。",
        2012, DIFFICULTY_ADVANCED,
    ),
    RegressionProblem(
        "cumcm-2014-C", CATEGORY_PREDICTION, "生猪养殖场的经营管理",
        "养猪场依据市场猪价与成本（猪苗、饲料价格）决定养殖经营规模：养殖周期、"
        "存栏与出栏时机会影响利润。基于历史价格数据预测未来猪价走势，建立养殖场"
        "经营决策模型，确定存栏规模与出栏策略使总利润最大，并讨论价格波动风险"
        "下的稳健策略。",
        "模型族：价格时序预测 + 库存/出栏决策优化。关键检验点：养殖周期约束；"
        "预测与决策两段衔接；风险情景讨论。",
        2014, DIFFICULTY_ADVANCED,
    ),
    RegressionProblem(
        "cumcm-2016-C", CATEGORY_PREDICTION, "电池剩余放电时间预测",
        "同一种电池在不同放电电流下，电压随时间衰减的曲线族已知。建立电池剩余放电"
        "时间的预测模型：由当前电压与放电电流推断剩余放电时间，并给出预测的误差"
        "估计；对附件中给定的放电状态完成预测。",
        "模型族：参数化衰减曲线拟合 / 插值外推。关键检验点：电流-时间曲线族的"
        "参数化；预测区间与误差估计；留出验证。",
        2016,
    ),
    RegressionProblem(
        "cumcm-2024-C", CATEGORY_PREDICTION, "农作物种植策略",
        "某乡村耕地分为露地（平旱/梯田/山坡/水浇地）与智慧大棚，适宜种植多种作物，"
        "各类作物有单产、成本、销售价格与季节约束，且存在轮作与连作限制。基于"
        " 2023 年及既往数据预测各类作物未来销量与价格趋势，给出 2024~2030 年"
        "逐年种植方案，使总收益最大并讨论不确定性影响。",
        "模型族：销量/价格预测 + 多周期种植计划优化。关键检验点：轮作约束建模；"
        "不确定性（情景/期望值）处理；多年期方案可行性。",
        2024,
    ),
    # ---- 机理类（5 道）----
    RegressionProblem(
        "cumcm-2018-A", CATEGORY_MECHANISM, "高温作业专用服装设计",
        "专用服装由三层材料（外界-第I层-第II层-空气层-皮肤）构成。建立热传导"
        "微分方程模型描述假人皮肤层温度随时间与厚度方向的变化；在 60 分钟作业内"
        "假人皮肤外侧温度不超过 47℃、超过 44℃ 的时间不超过 5 分钟的约束下，"
        "确定第 II 层材料的最优厚度。",
        "模型族：多层介质热传导偏微分方程 + 数值解（有限差分）。关键检验点："
        "分层边界条件与初始温度场；温度-厚度灵敏度；约束验证。",
        2018, DIFFICULTY_ADVANCED,
    ),
    RegressionProblem(
        "cumcm-2019-A", CATEGORY_MECHANISM, "高压油管的压力控制",
        "燃油经高压油泵进入细油管，油管内压力与燃油密度呈非线性关系（弹性模量"
        "随压力变化），单向阀开关使供油呈周期脉冲。建立油管内压力随时间变化的"
        "微分方程模型：设计单向阀每次开启的时长方案，使管内压力在给定时间内"
        "精确达到目标值且进入稳态后波动最小。",
        "模型族：流体连续性/本构方程 + 周期控制。关键检验点：压力-密度非线性"
        "本构；周期供油的稳态分析；控制方案收敛到目标压力。",
        2019, DIFFICULTY_ADVANCED,
    ),
    RegressionProblem(
        "cumcm-2020-A", CATEGORY_MECHANISM, "炉温曲线优化",
        "电子元件回焊炉内各温区维持设定温度，焊接区域中心的温度随传送带过炉"
        "速度与各温区设定变化。建立炉内传热模型计算中心温度曲线（炉温曲线）；"
        "在制程界限（升温/降温斜率、峰值温度范围、特定温度段持续时间）约束下，"
        "确定最优过炉速度，并讨论能否通过炉温区域设定进一步优化。",
        "模型族：热平衡/牛顿冷却方程 + 数值积分。关键检验点：温区温度插值；"
        "制程界限约束逐项验证；过炉速度寻优。",
        2020, DIFFICULTY_ADVANCED,
    ),
    RegressionProblem(
        "cumcm-2021-B", CATEGORY_MECHANISM, "乙醇偶合制备C4烯烃",
        "化工实验在不同温度与催化剂组合下测得乙醇转化率与 C4 烯烃收率/选择性"
        "数据。建立收率与温度、催化剂组合的定量关系模型；针对给定收率与选择性"
        "目标，给出最优温度与催化剂组合条件，并讨论模型的外推能力与实验验证"
        "方案。",
        "模型族：动力学/回归拟合（含交互项）。关键检验点：温度-收率非线性关系"
        "拟合优度；催化组合编码；条件寻优与留出验证。",
        2021, DIFFICULTY_ADVANCED,
    ),
    RegressionProblem(
        "cumcm-2022-A", CATEGORY_MECHANISM, "波浪能最大输出功率设计",
        "波浪能装置由浮子与振子组成两自由度振动系统，浮子在波浪激励下经弹簧/"
        "阻尼（PTO）与振子耦合，输出电功率。建立浮子与振子的运动微分方程组，"
        "对不同波浪频率与 PTO 阻尼系数求解系统响应，计算平均输出功率并确定"
        "使输出功率最大的阻尼参数。",
        "模型族：二阶常微分方程组 + 数值积分。关键检验点：两自由度耦合方程；"
        "激励项频率响应；平均功率积分与寻优。",
        2022, DIFFICULTY_ADVANCED,
    ),
)


def category_distribution(problems: tuple[RegressionProblem, ...] = REGRESSION_SET) -> dict[str, int]:
    """题型分布统计。"""
    dist: dict[str, int] = {}
    for p in problems:
        dist[p.category] = dist.get(p.category, 0) + 1
    return dist


def stratified_sample(
    per_category: int | None = None,
    problems: tuple[RegressionProblem, ...] = REGRESSION_SET,
) -> tuple[RegressionProblem, ...]:
    """分层抽样：每类题型取前 per_category 道（None=全量），保持类内登记顺序。"""
    if per_category is None:
        return problems
    counts: dict[str, int] = {}
    sampled: list[RegressionProblem] = []
    for p in problems:
        if counts.get(p.category, 0) < per_category:
            counts[p.category] = counts.get(p.category, 0) + 1
            sampled.append(p)
    return tuple(sampled)


def _statement_digest(problem: RegressionProblem) -> str:
    payload = json.dumps(
        {
            "business_id": problem.business_id,
            "category": problem.category,
            "title": problem.title,
            "year": problem.year,
            "difficulty": problem.difficulty,
            "statement": problem.statement,
            "reference_digest": problem.reference_digest,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def manifest(problems: tuple[RegressionProblem, ...] = REGRESSION_SET) -> dict[str, Any]:
    """冻结清单：逐题内容哈希 + 集合级 set_sha256（冻结后用于校验样本未变）。"""
    entries = [
        {
            "business_id": p.business_id,
            "category": p.category,
            "year": p.year,
            "sha256": _statement_digest(p),
        }
        for p in problems
    ]
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "count": len(problems),
        "categories": category_distribution(problems),
        "problems": entries,
        "set_sha256": sha256(canonical.encode("utf-8")).hexdigest(),
    }
