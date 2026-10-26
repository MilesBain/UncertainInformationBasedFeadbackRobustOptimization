# 输出 JSON 字段对照手册

本文档逐个解释 `results_io.py` 最终写出的 JSON 文件中**每一个字段**的含义、单位、计算方式和物理意义。目的不是复述代码，而是回答"这个数从哪来、在物理上代表什么、能不能拿它当结论"。

- 覆盖文件：一次成功运行目录下的 `summary.json`、`parameters.json`、`solver_and_checks.json`、`manifest.json`，以及失败时的 `failure.json`。
- 生成入口：`main.run_simulation` 在全部时段执行完毕后调用 `results_io.save_results`，由 `write_json` 统一写盘。
- 数值示例：取自 `results/verified_run/`（完整 24 时段、23 次 M2 对偶求解、`--verify-all` 打开）。示例数字只用于说明读法，不同参数配置会有不同数值。
- 本手册是纯说明文档，不参与建模、不参与反馈控制，也不被任何脚本读取。

**阅读路线**：1 三类数字 → 2 单位与符号约定（必读）→ 3 `summary.json` → 4 `parameters.json` → 5 `solver_and_checks.json` → 6 `manifest.json` → 7 `failure.json` → 8 与 CSV/NPZ 对齐 → 9 实例读数 → 10 核对脚本 → 11 常见误读。

---

## 1. 先分清三类数字

同一个运行目录里的数并不是同一种东西。读任何字段前，先判断它属于哪一层：

| 层次 | 典型字段 | 单位 | 说明 |
| --- | --- | --- | --- |
| 优化模型内的量 | `objective`、`slack_*`、`predicted_*_sq`、`dual_*` | p.u.（功率、电压平方）或加权目标值 | 属于 LinDistFlow 线性鲁棒 MILP 的世界，含软约束与盒式不确定集合 |
| AC 真实量 | `ac_*`、`active_loss_kwh`、`voltage_pu`、`grid_p/grid_q` | p.u. 或工程单位（kW、kvar、kWh） | 由 PYPOWER 非线性潮流算出，是"真实执行"的结果 |
| 数值复核残差 | `verification_maxima`、`checks` | 多为 p.u.，也含纯数值残差 | 判断"代码是否严格实现了公式"，不判断"物理上是否合格" |

把这三层混在一起，是最常见的误读来源。例如 `predicted_slack_sum_sq_pu = 0` 只说明**线性模型在自己的不确定集合内不需要松弛**，不代表 AC 电压合格；`verification_maxima` 全为 1e-15 也只说明**残差算得对**，不说明电压在 0.93～1.05 p.u. 内。

---

## 2. 阅读前必须统一的约定

### 2.1 基准值与换算

全部 p.u. 值都基于三相功率基准 `S_base = 10 MVA`、线电压基准 `V_base = 12.66 kV`：

| 基准 | 数值 | 由来 |
| --- | --- | --- |
| `base_mva` | 10.0 MVA | `config.BASE_POWER_MVA` |
| `base_kv` | 12.66 kV（线电压） | `config.BASE_VOLTAGE_KV` |
| `base_kw` | 10000.0 kW | `base_mva × 1000` |
| `base_ohm` | 16.02756 Ω | `base_kv² / base_mva` |
| 电流基准 | ≈ 456.0 A | `base_mva / (√3 × base_kv)` |
| `dt` | 1.0 h | `config.TIME_STEP_HOURS` |

换算关系：

| 物理量 | JSON 内部单位 | 换算到工程单位 |
| --- | --- | --- |
| 有功功率 P | p.u. | × 10000 → kW |
| 无功功率 Q | p.u. | × 10000 → kvar |
| 能量 E | p.u.·h | × 10000 → kWh |
| 电压幅值 V | p.u. | × 12.66 → kV（线电压） |
| **电压平方 V²** | p.u. | **没有电压量纲**；在 V≈1 附近，ΔV ≈ ΔV² / 2 |
| 电阻 r、电抗 x | p.u. | × 16.02756 → Ω |

电压平方是最容易被误读的量。`1.1025` 不是 1.1025 kV，而是 `(1.05 p.u.)²`；`0.035` 的 V² 误差在 V≈1.05 处只相当于约 `0.035 / 2.1 ≈ 0.017 p.u.`（约 0.21 kV）的幅值误差。

### 2.2 正方向

- **节点注入为正**：发电（光伏、储能放电）为正，负荷为负。`p_external`、`q_external`、`p_injected`、`q_injected`、`Bp`、`Bq` 都遵循这一约定。
- **支路潮流**：优化器中 `branch_p = -A @ p`，即"从根节点向下游送电为正"。因此重载时支路潮流为正，光伏倒送时为负。
- **根节点交换功率 `grid_p`、`grid_q` 为正表示"从上级电网吸收"**，为负表示"向上级电网倒送"。这两个量在 AC 中包含线路损耗，在优化器中不含损耗（见 3.7）。
- **弃光 `curt` 为正**表示被削减的可用出力；它在 `Bp` 中的系数是 `-1`，因为弃光减少注入。

### 2.3 索引

- **时段** `period = 0 … 23`，每个时段 1 h。时段 0 由静态初始化执行，1～23 由 M2 滚动执行。
- **节点列**：`node_index` 把物理母线 2～33 映射到列 0～31，根母线 1 不在任何列中。因此所有 `(24, 32)`、`(32, …)` 数组的第二维顺序 = 母线 2, 3, …, 33。
- **支路行**：顺序与 `config.BRANCHES` 的行序完全一致，第 1 行是支路 1，第 32 行是支路 32。`UNCERTAIN_BRANCHES` 用的是这个 1 起编号。
- **控制列**：16 个控制量的顺序由 `controls` 决定，不能自行假定；请用 `control_index` 查列号。

### 2.4 三种“电压”

| 名称 | 字段 | 来源 | 用途 |
| --- | --- | --- | --- |
| AC 幅值 | `ac_voltage_*_pu`、`voltages.csv: actual_ac_voltage_pu` | PYPOWER | 判断真实过压/欠压 |
| AC 平方 | `measured_voltage_sq`、`voltage_sq` | PYPOWER 幅值平方 | 作为 M2 的反馈状态、与预测比较 |
| 预测平方 | `predicted_*_sq` | LinDistFlow + 盒式不确定集合 | 优化器内部的鲁棒区间，**不是**统计置信区间 |

### 2.5 JSON 的编码约定

`write_json` 固定使用：UTF-8（无 BOM）、`ensure_ascii=False`（中文直接可读）、`indent=2`、`allow_nan=False`（出现 NaN/Inf 会直接报错，所以文件里不会出现非法数值）。NumPy 数组转为嵌套列表，NumPy 标量转为 Python 标量，**字典键一律转成字符串**，因此 `control_index` 的键是 `"('curt', 18)"` 这种形式，`node_index`、`parent_branch` 的键是 `"2"`、`"3"` 这样的字符串。

---

## 3. `summary.json`

整次运行的结论。由 `save_results` 在写盘前重新计算，**不读取任何旧结果**。

### 3.1 结构

```json
{
  "completed": true,
  "executed_periods": 24,
  "rolling_solves": 23,
  "all_executed_ac_converged": true,
  "all_checks_passed": true,
  "parameter_fingerprint_before": "<sha256>",
  "parameter_fingerprint_after": "<sha256>",
  "initialization_full_horizon_objective": 0.3030658284469552,
  "rolling_objective_sum": 0.0,
  "ac_voltage_min_pu": 0.930099984480138,
  "ac_voltage_max_pu": 1.0583705497444489,
  "ac_lower_violation_node_periods": 0,
  "ac_upper_violation_node_periods": 4,
  "ac_voltage_violation_tolerance_pu": 1e-07,
  "curtailment_kwh": 0.0,
  "active_loss_kwh": 8509.785624788592,
  "ac_realized_objective_same_weights": 3.872040635565588,
  "predicted_slack_sum_sq_pu": 0.0,
  "max_nominal_prediction_error_sq_pu": 0.03504777171743334,
  "ac_outside_predicted_interval_node_periods": 546,
  "ac_grid_p_limit_violation_periods": 0,
  "ac_grid_q_limit_violation_periods": 18,
  "ac_grid_q_min_kvar": -1435.7140969626414,
  "ac_grid_q_max_kvar": 2309.642785945414,
  "verification_maxima": { "...": "见 3.8" },
  "interpretation": "固定中文说明"
}
```

### 3.2 字段总表

| 字段 | 类型 | 单位 | 计算方式 | 物理含义 |
| --- | --- | --- | --- | --- |
| `completed` | bool | — | 字面量 `True` | 已走到保存阶段。失败时不会写出本文件（见第 7 节） |
| `executed_periods` | int | 个 | `len(records)` | 真正执行过的时段数，完整日为 24 |
| `rolling_solves` | int | 次 | `len(records) - 1` | M2 滚动求解次数；时段 0 属于静态初始化，不计入 |
| `all_executed_ac_converged` | bool | — | 字面量 `True` | 执行流保证：任一时段 AC 不收敛会抛异常，走不到保存 |
| `all_checks_passed` | bool | — | 字面量 `True` | 同上：任一复核残差超 `VALIDATION_TOL` 会抛 `AssertionError` |
| `parameter_fingerprint_before` | str | — | 运行开始时的 `data.fingerprint()` | 参数快照的 SHA-256，用于证明本次运行物理环境未被中途改动 |
| `parameter_fingerprint_after` | str | — | 写盘前再算一次 | 与 `before` 相等才算一致；不等会直接报错 |
| `initialization_full_horizon_objective` | float | 加权目标值 | `schedule.info['objective']` | 静态初始化 MILP 的 **24 时段总目标** |
| `rolling_objective_sum` | float | 加权目标值 | Σ 时段 1～23 的 `info['objective']` | 滚动阶段单时段目标之和，**不含时段 0** |
| `ac_voltage_min_pu` | float | p.u. | 24×32 个非根节点幅值的最小值 | AC 实际最低电压 |
| `ac_voltage_max_pu` | float | p.u. | 同上取最大 | AC 实际最高电压 |
| `ac_lower_violation_node_periods` | int | 节点·时段 | `count(V < 0.93 - 1e-7)` | 真实欠压的"节点×时段"个数 |
| `ac_upper_violation_node_periods` | int | 节点·时段 | `count(V > 1.05 + 1e-7)` | 真实过压的"节点×时段"个数 |
| `ac_voltage_violation_tolerance_pu` | float | p.u. | 固定常数 `1e-7` | 计数时扣除的数值噪声门槛 |
| `curtailment_kwh` | float | kWh | Σ 弃光 p.u. × 10000 × 1 h | 弃光电量 |
| `active_loss_kwh` | float | kWh | Σ AC 网损 p.u. × 10000 × 1 h | AC 全网有功损耗电量，含支路电阻损耗 |
| `ac_realized_objective_same_weights` | float | 加权目标值 | `100 × Σ(V²超限) + 1 × Σ弃光` | 用**同一套权重**给 AC 实测结果"重新计价" |
| `predicted_slack_sum_sq_pu` | float | p.u.（电压平方） | Σ 所有时段的 `slack_upper + slack_lower` | 优化器为满足电压软约束所购买的松弛量总量 |
| `max_nominal_prediction_error_sq_pu` | float | p.u.（电压平方） | `max(abs(V²_AC - V²_预测标称))` | 线性预测的最大误差（模型误差，不是参数误差） |
| `ac_outside_predicted_interval_node_periods` | int | 节点·时段 | 落在 `[lower-2e-7, upper+2e-7]` 之外的个数 | 鲁棒预测区间的实际覆盖率 |
| `ac_grid_p_limit_violation_periods` | int | 时段 | `count(grid_p < -2 - 2e-7 或 > 2 + 2e-7)` | 含损耗的根节点有功越限时段数 |
| `ac_grid_q_limit_violation_periods` | int | 时段 | `count(grid_q < -0.15 - 2e-7 或 > 0.15 + 2e-7)` | 含损耗的根节点无功越限时段数 |
| `ac_grid_q_min_kvar` | float | kvar | `min(grid_q) × 10000` | 全天最小根节点无功（负值=向主网倒送无功） |
| `ac_grid_q_max_kvar` | float | kvar | `max(grid_q) × 10000` | 全天最大根节点无功（正值=从主网吸收） |
| `verification_maxima` | object | 见 3.8 | 逐时段 `checks` 取最大 | 全部数值复核残差的最坏值 |
| `interpretation` | str | — | 固定文字 | 提醒读者三类判断不可混用 |

### 3.3 三个“目标值”不是一回事

这是本文件最容易出错的地方。

1. **`initialization_full_horizon_objective`**：静态初始化模型对 24 个时段一起优化的总目标，量纲是"加权和"，即
   `100 × Σ_{t=0..23} (slack_upper + slack_lower) + 1 × Σ_{t=0..23} 弃光`。
   它是一个**计划值**，其中后 23 个时段的计划从未真正执行（只执行了第 0 时段动作）。
2. **`rolling_objective_sum`**：只累加真正执行 M2 的时段 1～23，每个时段是该时段的单时段目标。它和上面那个 24 时段总目标**不能直接比大小**，因为覆盖的时段数、决策信息都不同。
3. **`ac_realized_objective_same_weights`**：不是任何优化器输出，而是把**实测 AC 电压**和**实际弃光**代进同一套权重算出来的"事后价格"。它回答的问题是："如果当初的目标函数按真实结果结算，会得多少分？"

示例运行中：初始化总目标 `0.303066`，滚动目标和 `0.0`，而 AC 事后计价 `3.872`。三者的大小关系没有必然规律，不能拿"滚动目标和为 0"证明"运行完美"。

目标权重：`VOLTAGE_PENALTY = 100`（每个 p.u. 的电压平方越限），`CURTAILMENT_PENALTY = 1`（每个 p.u. 的弃光）。作为尺度感：0.01 p.u. 的电压平方越限（约 0.5% 电压偏差）罚 1，等价于 1 p.u.（10 MW）的弃光。因此优化器宁可大量弃光，也不愿放松 1% 量级的电压越限。

### 3.4 电压字段

- `ac_voltage_min_pu` / `max`：在 **24 个时段 × 32 个非根节点 = 768** 个"节点·时段"上取极值。根节点固定为 1.0，不参与统计。
- 越限计数是"节点·时段"计数，不是"时段"计数：同一小时里 3 个母线过压，就算 3。
- 门槛 `ac_voltage_violation_tolerance_pu = 1e-7` 是结果文件里写死的小容差，用来避免把 `1.0500000001` 这种浮点噪声算成越限。它和优化容差、复核容差 `VALIDATION_TOL` 不是同一个数。
- **电压是软约束**：MILP 允许用 `slack_upper/slack_lower` 买越限，代价是 100 倍权重。因此"优化成功"与"AC 电压合格"是两件事，`ac_lower/upper_violation_node_periods` 才是物理结论。

示例运行：`0.930100 ～ 1.058371 p.u.`，欠压 0 个、过压 4 个（4 / 768 ≈ 0.52%）。最大过压出现在母线 18、时段 16。注意最小值 0.9301 只是**恰好**略高于 0.93，不代表有 0.0001 的安全裕度设计——AC 结果不受优化器直接约束。

### 3.5 电量字段

- `curtailment_kwh`：把 4 台光伏的弃光功率（p.u.）按时段累加，再乘 `base_kw × dt`。示例为 0，说明全天没有弃光。
- `active_loss_kwh`：AC 潮流的全网有功损耗（各支路首末端有功之和），乘 `base_kw × dt`。示例 `8509.786 kWh`，即 `0.851 p.u.·h`，平均损耗功率约 `354.6 kW`；作为参照，该日负荷电量约 `65.31 MWh`（3715 kW 额定 × 负荷曲线积分 17.58 h），损耗约占负荷电量的 13%。重载与光伏倒送时段损耗最大（`periods.csv` 中 t=14 达 1116 kW）。
- 这两个字段都可以用 `periods.csv` 的 `loss_kw`、`curtailment_kw` 逐行相加（`dt = 1 h`）精确复现。

### 3.6 线性预测 vs AC 实测

这是理解 M2 局限性的核心一组字段。

- `predicted_slack_sum_sq_pu`：优化器在**线性模型 + 盒式不确定集合**内为电压软约束付出的松弛量。示例为 0，说明优化器认为"在所有允许的线路参数取值下，电压都能满足 0.93～1.05"，不需要松弛。
- `max_nominal_prediction_error_sq_pu`：预测标称电压平方与 AC 实测电压平方的最大绝对差。示例 `0.035048`。
- `ac_outside_predicted_interval_node_periods`：AC 实测落在鲁棒区间 `[lower, upper]` 之外的"节点·时段"数。示例 `546 / 768 ≈ 71%`。

示例中的具体对照（母线 18，单位均为 p.u. 的电压平方）：

| 时段 | AC V | AC V² | 预测标称 V² | 预测区间 | 结论 |
| --- | --- | --- | --- | --- | --- |
| 16 | 1.058371 | 1.120148 | 1.085100 | [1.067701, 1.102500] | 高于上限 0.017648，超 1.05 限值 |
| 14 | 1.046270 | 1.094680 | 1.100465 | [1.098430, 1.102500] | 低于下限 0.003750（模型高估电压） |

两行说明误差**方向不固定**：t=14 线性模型高估电压，t=16 低估电压。误差来源不是线路参数（本次运行的真值恰好等于标称值），而是：

1. LinDistFlow **忽略线路损耗**，`V²` 只随 `rP + xQ` 线性变化，而真实潮流里有 `r(P²+Q²)` 项；
2. 滚动递推是**一阶增量**：`V²_{t+1} = V²_{t,实测} + H·θ`，每步的截断误差会累积；
3. 预测区间只覆盖**阻抗不确定性**，不覆盖**模型误差**。

因此 `predicted_slack_sum_sq_pu = 0` 的正确读法是"在线性鲁棒模型内部自洽"，不能读成"AC 电压合格"。示例中 4 个过压节点时段正是模型误差造成的。

这 4 个越限节点时段，也正是 `ac_realized_objective_same_weights = 3.872` 的**全部**来源：

| 节点·时段 | AC V | AC V² | 超出 `1.1025` 的部分 | 目标贡献（×100） |
| --- | --- | --- | --- | --- |
| 15, 33 | 1.052977 | 1.108761 | 0.006261 | 0.626 |
| 16, 17 | 1.051712 | 1.106099 | 0.003599 | 0.360 |
| 16, 18 | 1.058371 | 1.120148 | 0.017648 | 1.765 |
| 16, 33 | 1.055325 | 1.113712 | 0.011212 | 1.121 |
| 合计 | | | 0.038720 | 3.872 |

四行相加（`0.038720 × 100`）恰好等于 `summary` 中的 `3.8720406`。这也是把 JSON 结论与 `voltages.csv` 逐行对上的标准做法：先在明细里找出越限行，再乘权重求和。

另一个值得注意的现象：t=14、t=16 母线 18 的预测上限恰好等于 `1.1025 = 1.05²`。这说明**鲁棒最优解把最坏情况顶到了限值上**——模型认为"最坏情况刚好达标"，没有留任何裕度。此时真实值只要比模型的最坏情况预测再高一点，就会越限。这也解释了为什么"零松弛"的运行仍会出现过压。

`max_nominal_prediction_error_sq_pu = 0.035048` 就是母线 18、时段 16 的 `1.120148 - 1.085100`；换算成幅值约 `0.035048 / (2 × 1.05) ≈ 0.0167 p.u.`，即约 `0.21 kV`。

### 3.7 主网限值字段

优化器里的 `grid_p`、`grid_q` 是**无损功率平衡**下的根节点交换功率（`device_model.add_devices` 直接对注入求和），而 AC 里的 `grid_p`、`grid_q` 是**含网损**的真实交换功率，两者不应相等，也不应互相校验。

- `ac_grid_p_limit_violation_periods`：示例 0。限值是 ±20000 kW（优化器中为 ±2.0 p.u.），非常宽，不难满足。
- `ac_grid_q_limit_violation_periods`：示例 **18 / 24**。限值是 ±1500 kvar（优化器中为 ±0.15 p.u.）。
- `ac_grid_q_min_kvar = -1435.714`、`ac_grid_q_max_kvar = 2309.643`：最小值没有触及 -1500，最大值超出 +1500 约 810 kvar。

物理解释：本算例的 AC 网络没有并联充电电纳，线路串联电抗上的无功损耗 `Σ x·I²` **恒为正**。对同一时段、同一组动作，`Q_AC = Q_无损模型 + Q_损耗`。于是：

- 吸收方向（正）更容易突破 +1500 kvar 上限——示例中 18 个时段越限；
- 倒送方向（负）反而更不容易突破 -1500 kvar 下限——因为 `Q_AC ≥ Q_无损模型 ≥ -1500`，仅由损耗不可能造成下限越限，示例中最小值只有 -1435.7。

这不是优化器出错，而是"优化器约束的是无损口径、AC 报告的是含损口径"的必然结果，`验证说明.md` 也明确写了"主网限值在优化器中约束无损功率；AC 潮流含线路损耗，实际超限需要单独报告"。

### 3.8 `verification_maxima`

对每个时段 `record['checks']` 里的每一项取全局最大值（初值 0）。因此它是**本次运行中所有数值复核残差的最坏值**，全部非负。

- 键的集合取决于运行方式：`initial_*` 只出现在时段 0；`dual_absolute_objective` 只在做了绝对值等价复算的时段出现（`--verify-all` 时每步都有，默认只在 t=1、6、12、18、23 出现）。
- 每一项的具体公式见 5.5，那里与 `solver_and_checks.json` 的 `checks` 一一对应。
- 判读标准：全部小于本次运行的 `VALIDATION_TOL`（示例为 `2e-7`，可由 `parameters.json.config.VALIDATION_TOL` 查到）。示例中最大的是 `ac_nodal_balance = 1.02e-10`，仍远低于阈值。

### 3.9 `interpretation`

固定中文说明，不随运行变化：

> MILP软约束可行、AC收敛、实际电压合格是不同判断。初始化目标为24时段总值；滚动目标和不含时段0。

它的作用是防止把 3.3、3.4、3.6 里的不同结论混为一谈。

### 3.10 用它做判断的顺序

1. 两个指纹是否相等 → 本次运行物理环境是否稳定；
2. `verification_maxima` 是否全部小于容差 → 代码是否严格实现了公式；
3. `ac_*_violation_node_periods`、`ac_grid_*_violation_periods` → 物理上是否真的合格；
4. `predicted_slack_sum_sq_pu`、`ac_outside_predicted_interval_node_periods` → 线性模型与 AC 的差距有多大；
5. 只有 1、2 通过而 3 有越限时，正确结论是"数值实现正确，但 AC 运行存在越限"，而不是"验证失败"或"结果无意义"。

---

## 4. `parameters.json`

一次运行的**参数与矩阵快照**，由 `data.snapshot()` 生成，用于脱离代码复算。它包含四个顶层键：

```json
{
  "config":     { "32 个大写配置常量": "原始工程单位" },
  "arrays":     { "22 个 NumPy 数组": "标幺值，二维/三维嵌套列表" },
  "attributes": { "26 个标量/字典/列表": "派生属性" },
  "network":    { "10 个拓扑量": "径向网络结构" }
}
```

### 4.1 `config`：原始配置（工程单位）

只收集 `config.py` 中**名字全大写**的常量，保持工程单位（kW、kvar、kWh、Ω）。这是"人写的输入"，不是优化用的标幺值。

| 字段 | 示例 | 含义 |
| --- | --- | --- |
| `BASE_POWER_MVA` | 10.0 | 三相功率基准 |
| `BASE_VOLTAGE_KV` | 12.66 | 线电压基准 |
| `ROOT_BUS` | 1 | 松弛节点（上级电网接入点） |
| `BUS_COUNT` | 33 | 母线总数 |
| `PERIOD_COUNT` | 24 | 时段数（小时） |
| `TIME_STEP_HOURS` | 1.0 | 时段长度，能量与功率的换算系数 |
| `ROOT_VOLTAGE_PU` | 1.0 | 根节点电压设定，AC 中作为 REF 节点电压 |
| `MIN_VOLTAGE_PU` | 0.93 | 电压下限（软约束） |
| `MAX_VOLTAGE_PU` | 1.05 | 电压上限（软约束） |
| `VOLTAGE_PENALTY` | 100.0 | 每 p.u. 电压平方越限的惩罚权重 |
| `CURTAILMENT_PENALTY` | 1.0 | 每 p.u. 弃光的惩罚权重 |
| `GRID_P_LIMIT_KW` | [-20000, 20000] | 根节点有功交换限值（优化器中无损口径） |
| `GRID_Q_LIMIT_KVAR` | [-1500, 1500] | 根节点无功交换限值（优化器中无损口径） |
| `PV_MIN_POWER_FACTOR` | 0.95 | 逆变器最小功率因数；由此得 `pv_factor = tan(arccos 0.95) = 0.328684`，即每 1 p.u. 有功可配 0.3287 p.u. 无功 |
| `BRANCHES` | 32 行 `[from, to, r_Ω, x_Ω]` | 支路表；行号即支路编号（1 起）；要求 r、x > 0 |
| `LOADS` | `{"2": [100, 60], ...}` | 母线 → (额定有功 kW, 额定无功 kvar)，共 32 条，根节点无负荷 |
| `LOAD_PROFILE` | 24 个数 | 负荷倍率曲线，最大 1.0（第 19 个点），用于乘 `LOADS` |
| `PV_PROFILE` | 24 个数 | 光伏可用出力倍率曲线（0～1），用于乘装机容量 |
| `PV_CAPACITY_KW` | `{"18": 3000, ...}` | 光伏装机（4 个母线：18、22、25、33） |
| `SVG_CAPACITY_KVAR` | `{"18": 600, ...}` | SVG 容量（母线 18、33 各 ±600 kvar） |
| `CAPACITORS` | `{"6": [150, 4], ...}` | 母线 → (单档容量 kvar, 档位数)；母线 6、12、24、30 |
| `STORAGE` | 见本节末 | 储能参数（母线 18） |
| `UNCERTAIN_BRANCHES` | `{"5": 0.3, "12": 0.3, "16": 0.3, "29": 0.3}` | 不确定支路号 → 相对半径；r、x 各自按该比例上下浮动 |
| `TRUE_RESISTANCE_OHM` | 32 个数 | AC 仿真使用的真实电阻（本算例等于 `BRANCHES` 的 r） |
| `TRUE_REACTANCE_OHM` | 32 个数 | AC 仿真使用的真实电抗 |
| `SOLVER_THREADS` | 1 | Gurobi 线程数（固定以保证可复现） |
| `SOLVER_SEED` | 0 | Gurobi 随机种子 |
| `SOLVER_MIP_GAP` | 1e-8 | 相对 MIP 间隙 |
| `SOLVER_MIP_GAP_ABS` | 1e-10 | 绝对 MIP 间隙 |
| `SOLVER_FEASIBILITY_TOL` | 1e-8 | 可行性容差 |
| `SOLVER_INTEGER_TOL` | 1e-8 | 整数容差 |
| `VALIDATION_TOL` | 2e-7（该次运行） | 独立复核阈值；不同运行可能不同 |

`STORAGE` 的每个储能包含：`p_ch_max`（最大充电功率 kW）、`p_dis_max`（最大放电功率 kW）、`e_capacity`（容量 kWh）、`q_max`（无功上下限 kvar）、`eta_ch` / `eta_dis`（充放电效率）、`soc_min` / `soc_max`（SOC 上下限）、`soc_initial`（初始 SOC）。

注意：`config` 只保存大写常量。若将来在 `config.py` 里新增小写参数，它不会出现在本文件中。

### 4.2 `arrays`：优化实际使用的标幺数组

形状中的 `n = 32`（非根节点数），`2n = 64`（r 与 x 拼接），`C = 16`（控制数），`T = 24`（时段数）。

| 字段 | 形状 | 单位 | 定义与物理含义 |
| --- | --- | --- | --- |
| `grid_p_limits` | (2,) | p.u. | `[-2, 2]`，即 ±20 MW；根节点有功交换上下限 |
| `grid_q_limits` | (2,) | p.u. | `[-0.15, 0.15]`，即 ±1500 kvar |
| `p_load` | (T, n) | p.u. | `P_rated[kW] / 1000 / 10 × LOAD_PROFILE[t]`；逐时段逐节点有功负荷 |
| `q_load` | (T, n) | p.u. | 同上，无功负荷 |
| `pv_available` | (T, n) | p.u. | `PV_PROFILE[t] × 装机[kW] / 1000 / 10`；光伏可用最大出力（未弃光时） |
| `p_external` | (T, n) | p.u. | `pv_available - p_load`；设备动作之外的净有功注入（正=发电） |
| `q_external` | (T, n) | p.u. | `-q_load`；设备动作之外的无功注入（负荷消耗无功，故为负） |
| `A` | (n, n) | 0/1 | 路径矩阵：`A[b, i] = 1` 当且仅当支路 b 位于根节点到节点 i 的路径上（行=支路序，列=母线 2..33） |
| `Bp` | (n, C) | p.u./p.u. | 控制量 → 节点有功注入的映射；`curt` 为 `-1`（弃光减少注入），`pess` 为 `+1`（放电增加注入） |
| `Bq` | (n, C) | p.u./p.u. | 控制量 → 节点无功注入的映射；`qpv`、`qsvg`、`qcb`、`qess` 为 `+1` |
| `r_nominal` | (n,) | p.u. | 支路电阻标幺值 = `r_Ω / base_ohm` |
| `x_nominal` | (n,) | p.u. | 支路电抗标幺值 |
| `r_radius` | (n,) | p.u. | 电阻不确定半径 = `UNCERTAIN_BRANCHES` 比例 × `r_nominal`；其余支路为 0 |
| `x_radius` | (n,) | p.u. | 电抗不确定半径 |
| `theta_nominal` | (2n,) | p.u. | `[r_nominal; x_nominal]`，参数向量的中心 |
| `theta_radius` | (2n,) | p.u. | `[r_radius; x_radius]`，盒式不确定集合的半径 |
| `theta_lower` | (2n,) | p.u. | `theta_nominal - theta_radius` |
| `theta_upper` | (2n,) | p.u. | `theta_nominal + theta_radius` |
| `F` | (4n, 2n) = (128, 64) | — | `vstack([I, -I])`，与 `g` 一起表示盒式约束 `F·θ ≤ g` |
| `g` | (4n,) = (128,) | p.u. | `[theta_upper; -theta_lower]`；即 `θ ≤ θ_upper` 且 `-θ ≤ -θ_lower` |
| `K` | (n, 2n, C) | —（比值） | 控制灵敏度：`∂(电压平方增量系数)/∂u`，见下文 |
| `initial_energy` | (储能数,) | p.u.·h | `soc_initial × e_capacity / base_kw`；示例 `0.5 × 2000 / 10000 = 0.1`（即 1000 kWh） |

**LinDistFlow 与 `A` 的关系（理解全部电压字段的基础）**。对非根节点 i：

```text
V_i² = V_0² + 2 · Σ_b A[b,i] · ( r_b · (A p)_b + x_b · (A q)_b )
```

其中 `(A p)_b` 是支路 b 下游的净注入之和（正=发电），而支路实际潮流是它的相反数。代码把后者写成 `coefficient_matrix(p, q)[i, b] = 2 · A[b,i] · (A p)_b`，于是

```text
V² = V_0² + H_r · r + H_x · x = V_0² + H · θ
```

这就是 `predicted_nominal_sq` 的来源，也是"电压平方对参数的线性关系"的确切含义。因为没有损耗项，`H` 只依赖**节点上游的潮流**，不依赖电压本身。

**`K` 的物理意义**。从时段 t 到 t+1，注入变化来自两部分：外部负荷/光伏变化（`H0 = coefficient_matrix(Δp_ext, Δq_ext)`）和设备动作变化（`Δu`）。由于注入对 `u` 是线性的，增量系数矩阵可写为

```text
H = H0 + K·Δu
```

`K` 刻画的是：**当设备设定值变化时，该节点电压对每条线路参数的敏感度如何变化**，即 `Δu` 与 `θ` 的交叉项。它的元素由路径矩阵和 `Bp`、`Bq` 复合而成：

```text
K[i, b, c]     = 2 · A[b,i] · (A·Bp)[b,c]
K[i, n+b, c]   = 2 · A[b,i] · (A·Bq)[b,c]
```

有了 `H`，鲁棒区间就是盒式集合上的精确最坏情况：

```text
center_i = V²_i(t,实测) + H_i · θ_nominal
radius_i = Σ_k |H[i,k]| · θ_radius[k]
predicted_lower_sq = center - radius
predicted_upper_sq = center + radius
```

`radius` 用绝对值加权，正是盒式集合的支持函数；因此 `predicted_lower/upper_sq` 是该线性模型下的**精确**上下界（不是保守近似）。`solver_and_checks.json` 里的 `vertex_lower_bound` / `vertex_upper_bound` 就是拿 256 个顶点直接枚举来验证这两个式子。示例中 4 条不确定支路各有 r、x 两个方向，共 8 个自由维度，故顶点数为 `2^8 = 256`。

### 4.3 `attributes`：派生标量与字典

| 字段 | 示例 | 含义 |
| --- | --- | --- |
| `node_index` | `{"2": 0, ..., "33": 31}` | 物理母线 → 数组列号（0 起） |
| `n` | 32 | 非根节点数 |
| `periods` | 24 | 时段数 |
| `dt` | 1.0 | 时段长度 h |
| `base_mva` / `base_kv` / `base_kw` / `base_ohm` | 10 / 12.66 / 10000 / 16.02756 | 基准值 |
| `root_voltage` | 1.0 | 根节点电压 p.u. |
| `voltage_min` / `voltage_max` | 0.93 / 1.05 | 电压限值 p.u. |
| `voltage_min_sq` / `voltage_max_sq` | 0.8649 / 1.1025 | 限值的平方，优化器直接使用 |
| `voltage_weight` / `curtailment_weight` | 100 / 1 | 目标权重 |
| `pv_factor` | 0.328684 | `tan(arccos 0.95)`；每 p.u. 光伏有功可提供的无功能力上限 |
| `storage` | 见 4.1 | 储能参数的副本（工程单位） |
| `pv_buses` | [18, 22, 25, 33] | 光伏母线 |
| `svg_buses` | [18, 33] | SVG 母线 |
| `capacitor_buses` | [6, 12, 24, 30] | 电容器母线 |
| `storage_buses` | [18] | 储能母线 |
| `svg_limit` | `{"18": 0.06, "33": 0.06}` | SVG 无功上限 p.u.（±600 kvar） |
| `capacitors` | `{"6": [0.015, 4], ...}` | 母线 → [单档 p.u. (150 kvar), 档位数] |
| `controls` | 16 个 `[kind, bus]` | 控制列的定义与顺序 |
| `control_index` | `{"('curt', 18)": 0, ...}` | 控制 → 列号；JSON 中键是字符串 |
| `control_count` | 16 | 控制数 |

`controls` 的顺序是：4 个弃光（18、22、25、33）→ 1 个储能有功（18）→ 4 个光伏无功 → 2 个 SVG 无功 → 4 个电容器档位 → 1 个储能无功。**不要用记忆代替查表**，不同配置下位置会变。

### 4.4 `network`：拓扑

| 字段 | 含义 |
| --- | --- |
| `buses` | 1～33 全部母线 |
| `nonroot_buses` | 2～33，决定所有数组的列顺序 |
| `root_bus` | 1 |
| `branch_from` / `branch_to` | 每条支路的起点/终点母线（1 起） |
| `parent_branch` | 母线 → 其上游支路下标（0 起） |
| `children_branches` | 母线 → 下游支路下标列表 |
| `paths` | 母线 → 从根节点到该母线的支路下标列表 |
| `path_matrix` | (32, 32)，与 `arrays.A` 是同一个矩阵（序列化了两次） |
| `forward_order` | 支路下标按"离根节点深度"从浅到深排序，供前推回代潮流使用 |

### 4.5 指纹 `fingerprint`

`data.fingerprint()` 对 `snapshot()` 的 `config + arrays + attributes + network` 做规范化 JSON（`sort_keys=True`、`ensure_ascii=False`、`allow_nan=False`）后取 SHA-256。它只覆盖**数据**，不覆盖代码、库版本或求解器参数（库版本记在 `manifest.json`）。

用途：`main.py` 在运行开始、每个时段结束后、`save_results` 写盘前都比较指纹；一旦变化就抛 `AssertionError`，防止"滚动过程中参数被改写"这类静默错误。示例运行的前后指纹一致：

```text
777f5fc5c2c756f181b149d4f6bb3301cd3941593783c13c54f142ac209187f1
```

### 4.6 快照 ≠ 当前 `config.py`

`parameters.json` 记录的是**那一次运行**的参数。仓库里的 `config.py` 之后可能被修改，两者不应混用。当前仓库就存在这样的差异：

- 当前 `config.py` 的 `PV_PROFILE` 全为 1，而 `results/verified_run/parameters.json` 中是一条钟形曲线；
- 当前 `config.py` 的 `VALIDATION_TOL = 1e-6`，而 `verified_run` 保存的是 `2e-7`。

因此：**复现或引用某次结果时，一律以该结果目录下的 `parameters.json` 为准**，不要用当前 `config.py` 解释旧结果。指纹不同（`777f…` 与另一组结果中的 `2b73…`）也正说明参数已经变了。

---

## 5. `solver_and_checks.json`

每一步 MILP 的求解状态与**独立复核残差**。它回答"这一步真的求到最优了吗、求出来的解真的满足约束吗"，不回答"物理上好不好"。

### 5.1 结构

```text
{
  "initialization": { "status": "optimal", "gurobi_status": 2, "objective": 0.303...,
                      "gap": 0.0, "solver_seconds": 0.146, "variables": 6744,
                      "constraints": 8856, "binary_variables": 48,
                      "total_seconds": 1.605,
                      "purpose": "independently built static robust model; execute period 0 only" },
  "periods": [
    { "period": 0, "source": "static_initialization",
      "solver": { "...": "初始化信息 + period=0 + 单时段目标" },
      "checks": { "initial_devices": 3.58e-15, "...": "..." } },
    { "period": 1, "source": "M2_dual",
      "solver": { "...": "M2 求解信息" },
      "checks": { "device_constraints": 0.0, "...": "..." } }
  ]
}
```

### 5.2 `initialization`

静态初始化模型的求解摘要，来自 `solver_utils.optimize_checked` 加 `initialization.py` 的补充字段：

| 字段 | 含义 |
| --- | --- |
| `status` | 固定为 `"optimal"`；只有 `GRB.OPTIMAL` 才会继续，其余状态直接抛异常 |
| `gurobi_status` | Gurobi 状态码，`2 = OPTIMAL` |
| `objective` | **24 时段总目标**（加权值），与 `summary.initialization_full_horizon_objective` 相同 |
| `gap` | 相对 MIP 间隙；`0.0` 表示在 `MIPGap = 1e-8` 内已证明最优 |
| `solver_seconds` | Gurobi 的 `Runtime`（纯求解时间） |
| `variables` | 模型变量数（含连续、整数、二元） |
| `constraints` | 约束行数 |
| `binary_variables` | 二元变量数（每台储能每个时段 2 个充/放状态） |
| `total_seconds` | 从进入求解函数到后处理结束的墙钟时间（含建模、提取、重新计算区间） |
| `purpose` | 固定字符串：说明这是本项目独立建立的静态模型，只执行第 0 时段 |

### 5.3 `periods[]`

每个已执行时段一项，顺序与执行顺序一致：

| 字段 | 含义 |
| --- | --- |
| `period` | 时段号 0～23 |
| `source` | `"static_initialization"`（时段 0）或 `"M2_dual"`（滚动时段） |
| `solver` | 该时段的求解摘要，见 5.4 |
| `checks` | 该时段的复核残差，见 5.5 |

### 5.4 `solver` 子字段

M2 的求解信息在公共字段之外还包含：

| 字段 | 含义 |
| --- | --- |
| `status` / `gurobi_status` | 同上，只有最优才写入 |
| `objective` | **该时段**的目标值（电压惩罚 + 弃光惩罚） |
| `gap` | 该步的相对 MIP 间隙 |
| `solver_seconds` | 该步 Gurobi 求解时间 |
| `variables` / `constraints` / `binary_variables` | 该步模型规模。M2 每步重建，规模与初始化模型不同：示例中 M2 变量更多（8281 对 6744，因为多了 32×128 个对偶变量），约束更少（4201 对 8856） |
| `period` | 该步求解的目标时段 |
| `formulation` | `"dual"`（正式对偶式）或 `"absolute"`（绝对值等价式，仅用于核验，结果不写入正式轨迹） |
| `total_seconds` | 该步墙钟时间 |
| `voltage_penalty` | `100 × (slack_upper + slack_lower)`，该步电压惩罚分量 |
| `curtailment_penalty` | `1 × 该步弃光总量`，该步弃光惩罚分量 |

两个易错点：

1. **时段 0 的 `solver.objective` 不是 24 时段总目标**。`main.py` 在记录时段 0 时把目标替换成"仅该时段贡献"的值（示例为 `0.0`），而完整总目标 `0.303066` 保留在 `initialization.objective` 里。两者不能互相替代。
2. `voltage_penalty + curtailment_penalty` 应当等于 `objective`。`checks.objective_reconstruction` 就是在验证这一点。

### 5.5 `checks` 子字段

全部是"违反量"或"绝对差"，单位为 p.u.（电压平方或功率），非负，阈值是该次运行的 `VALIDATION_TOL`。命名规则：值越大越差，保存在文件里的都是**通过**的残差。

**时段 0 独有（对完整 24 时段静态计划的复核）**

| 字段 | 含义 | 公式要点 |
| --- | --- | --- |
| `initial_devices` | 24 时段全部设备/储能/主网无损约束的最大违反量 | 逐时段算 `device_constraints` 后取最大 |
| `initial_robust_voltage` | 静态计划在 256 个顶点上的电压软约束最大违反量 | `max(max_v V² - V²max - slack_up, V²min - slack_low - min_v V²)` |
| `initial_vertex_bounds` | 顶点枚举得到的上下界与记录的 `predicted_*` 的最大偏差 | 验证区间是精确的 |
| `initial_objective` | 重算的 24 时段目标与 `initialization.objective` 的差 | 验证目标函数组装正确 |

**每个时段都有**

| 字段 | 含义 | 公式要点 |
| --- | --- | --- |
| `device_constraints` | 该时段设备与无损主网的约束违反量 | 光伏容量/无功能力、SVG、电容档位整数性、储能功率/互斥/SOC/能量递推、根节点功率限值 |
| `increment_coefficient` | 直接由注入差算出的 `H` 与结果中 `coefficient` 的差 | `‖H_direct - H_result‖∞`，验证 `H = H0 + KΔu` |
| `vertex_upper_bound` | 256 顶点电压平方最大值与 `predicted_upper_sq` 的差 | 验证支持函数写法与枚举一致 |
| `vertex_lower_bound` | 256 顶点最小值与 `predicted_lower_sq` 的差 | 同上 |
| `robust_voltage` | 用顶点重算的软约束最大违反量（含 slack ≥ 0） | 验证 `upper ≤ V²max + slack_up`、`V²min - slack_low ≤ lower` |
| `dual_equality` | 对偶等式残差 | `‖Λ_up·F - H‖∞`、`‖Λ_low·F + H‖∞`，其中 `F = [I; -I]` |
| `dual_inequality` | 对偶不等式与 λ ≥ 0 的最大违反量 | `g·λ ≤ 上界 + slack - 实测`，且 `λ ≥ 0` |
| `objective_reconstruction` | 由动作重算的目标与 Gurobi 目标的差 | 验证目标函数 |
| `dual_absolute_objective` | 对偶式与绝对值式的目标差 | **只在做了等价复算的时段出现**；差值受 MIP 间隙限制，可能接近容差（超出会中止运行，故保存下来的必然在容差内） |

**AC 复核（每个时段都有）**

| 字段 | 含义 | 公式要点 |
| --- | --- | --- |
| `ac_device_mapping` | 按物理含义重构的注入与 `Bp/Bq` 映射的差 | 超过 `1e-7` 时 `run_ac` 会直接报错 |
| `ac_nodal_balance` | 用复电压和欧姆定律算出的节点注入与给定注入的差 | `max abs(S_calc - (p + jq))` |
| `ac_sweep_voltage` | PYPOWER 电压与独立前推回代电压的差 | 两套潮流算法互校 |
| `ac_global_p_balance` | 全网有功平衡残差 | `grid_p + Σp_inj - loss_p` |
| `ac_global_q_balance` | 全网无功平衡残差 | `grid_q + Σq_inj - loss_q` |
| `ac_root_voltage` | 根节点电压与 1.0 的差 | REF 节点是否被正确固定 |

关于对偶式的记号：盒式集合写成 `F·θ ≤ g`，对每个节点求"在盒内最大化 `H_i·θ`"的支持函数，其对偶为

```text
max g·λ   s.t.  Fᵀ·λ = H_i,  λ ≥ 0
```

这就是 `dual_upper` / `dual_lower`（形状 `(32, 128)`）的来历；`checks.dual_equality` 与 `dual_inequality` 分别验证 `Fᵀλ = ±H` 和 `g·λ ≤ 界 + slack - 实测`。

### 5.6 怎么用这份文件

- 想证明"求解器确实求到了最优"：看 `status`、`gurobi_status = 2`、`gap ≤ 1e-8`。
- 想证明"解确实满足模型"：看 `checks` 全部小于容差。
- 想证明"对偶式和绝对值式等价"：看 `dual_absolute_objective`。它在示例中为 `0.0`；在另一组配置的结果中为 `8.81e-7`，已接近当时的容差 `1e-6`。后者不是错误，而是 MIP 间隙导致的最优值差异——所以这个字段只能证明"在求解精度内等价"。
- 想排查失败：`main.py` 会把失败模型写成 `failure_diagnostics/<模型名>.lp`，不可行时再写 `.ilp`。

---

## 6. `manifest.json`

环境与可复现性记录。

| 字段 | 含义 |
| --- | --- |
| `python` | Python 版本（`platform.python_version()`），示例 `3.8.20` |
| `libraries` | `numpy`、`scipy`、`gurobipy`、`PYPOWER` 的已安装版本（`importlib.metadata.version`） |
| `options` | 本次运行的命令行选项：`rolling_steps`、`verify_all`、`show_log`、`time_limit` |
| `parameter_fingerprint` | 与 `summary` 中的指纹相同，便于跨文件对齐 |
| `source_sha256` | 项目根目录下所有 `*.py` 的 SHA-256（按文件名排序），包括 `results_io.py` 自己 |

用途：

1. **复现**：版本 + 选项 + 源码哈希三者一致，才能声称"同一次实验"。
2. **审计**：改过任何一个 `.py`，对应哈希都会变；`tests/` 子目录不在此列（`glob('*.py')` 不递归）。
3. 注意 `libraries` 只记录版本号，不记录 Gurobi 许可证类型或 CPU 型号；严格复现还需固定线程数（已由 `config.SOLVER_THREADS = 1` 保证）。

---

## 7. `failure.json`

只在运行失败、且输出目录是新建的、且 `summary.json` 尚未写出时，由 `main.main` 的异常分支写入：

| 字段 | 含义 |
| --- | --- |
| `completed` | 固定 `false` |
| `error` | 异常的字符串形式 |
| `type` | 异常类名，如 `RuntimeError`、`AssertionError`、`FileExistsError` |

注意：

- 如果失败发生在 `summary.json` 写出之后，本文件不会出现（判断条件是"`summary.json` 不存在"）。
- 若输出目录已有内容，`main` 会在建模前抛 `FileExistsError`，不会覆盖旧结果，也不会写 `failure.json`（因为目录不是本次新建的）。
- 失败时可能保留 `failure_diagnostics/` 下的 `.lp` / `.ilp` 模型文件，用于定位不可行原因。

---

## 8. 与 CSV / NPZ 的对应关系

JSON 是"结论"，CSV 是"逐时段 / 逐节点明细"，NPZ 是"全量数组"。同一目录下：

| JSON 字段 | 对应明细 |
| --- | --- |
| `ac_voltage_min_pu` / `max` | `voltages.csv` 的 `actual_ac_voltage_pu` 列取极值；或 `periods.csv` 的 `voltage_min_pu` / `voltage_max_pu` 再取极值 |
| `active_loss_kwh` | `periods.csv` 的 `loss_kw` 逐行求和（`dt = 1 h`） |
| `curtailment_kwh` | `periods.csv` 的 `curtailment_kw` 逐行求和 |
| `ac_grid_q_min_kvar` / `max` | `periods.csv` 的 `grid_q_kvar` 取极值 |
| `predicted_slack_sum_sq_pu` | `voltages.csv` 的 `slack_upper_sq_pu` + `slack_lower_sq_pu` 全表求和 |
| `max_nominal_prediction_error_sq_pu` | `voltages.csv` 中 `actual_ac_voltage_pu² - predicted_nominal_sq_pu` 的最大绝对值 |
| `verification_maxima` | `solver_and_checks.json` 各时段 `checks` 取最大 |

`trajectory.npz` 的数组（`np.load` 后可用 `files` 查看）：

| 数组 | 形状 | 含义 |
| --- | --- | --- |
| `control` | (T, 16) | 实际执行的控制量 p.u. |
| `charge_power` / `discharge_power` | (T, 储能数) | 充电/放电功率 p.u.（均非负） |
| `charge_on` / `discharge_on` | (T, 储能数) | 充/放互斥状态 0/1 |
| `capacitor_steps` | (T, 电容数) | 电容器整数档位 |
| `energy_after` | (T, 储能数) | 时段末能量 p.u.·h；`energy_before` 为时段初能量 |
| `voltage_pu` | (T, 32) | AC 电压幅值 p.u. |
| `measured_voltage_sq` | (T, 32) | AC 电压平方，作为反馈状态 |
| `predicted_nominal_sq` / `predicted_lower_sq` / `predicted_upper_sq` | (T, 32) | 线性模型的标称值与鲁棒区间 |
| `slack_upper` / `slack_lower` | (T, 32) | 电压软约束松弛量（电压平方 p.u.） |
| `grid_p` / `grid_q` | (T,) | AC 根节点交换功率 p.u.（正=吸收） |
| `p_injected` / `q_injected` | (T, 32) | AC 节点净注入 p.u. |
| `branch_p_from` / `branch_q_from` / `branch_p_to` / `branch_q_to` | (T, 32) | AC 支路首末端潮流 p.u. |
| `feedback_voltage_sq` / `previous_control` | (T-1, 32) / (T-1, 16) | 时段 1～23 的反馈输入（时段 0 无前序，故少一行） |
| `coefficient` | (T-1, 32, 64) | 每步的增量系数矩阵 `H` |
| `dual_upper` / `dual_lower` | (T-1, 32, 128) | 每步的对偶变量 λ |

`initial_static_plan.npz` 是 24 时段静态计划的完整存档（含 `slack_*` 与 `predicted_*`）；**只有第 0 行被执行**，其余行仅用于核验，不要把它当作实际轨迹。

`periods.csv` 的列：`period, source, objective, voltage_min_pu, voltage_max_pu, curtailment_kw, grid_p_kw, grid_q_kvar, loss_kw, solver_seconds`。注意 `objective` 列在时段 0 只是该时段贡献，与 `summary.initialization_full_horizon_objective` 不同。

---

## 9. 实例：怎么把 `verified_run` 读成物理结论

| 读数 | 值 | 解读 |
| --- | --- | --- |
| 指纹前后 | 一致（`777f…`） | 24 小时运行期间参数未被改动 |
| `verification_maxima` 最大值 | `1.02e-10` | 远小于容差 `2e-7`，数值实现正确 |
| `ac_voltage_min_pu` / `max` | 0.930100 / 1.058371 | 实际电压跨过 1.05，最低点刚好在 0.93 之上 |
| 欠压 / 过压节点时段 | 0 / 4（共 768） | 存在真实过压（t=15 母线 33，t=16 母线 17、18、33），占 0.52% |
| `curtailment_kwh` | 0 | 全天没有弃光，控制全部靠无功与储能 |
| `active_loss_kwh` | 8509.786 | 0.851 p.u.·h，平均 354.6 kW，约为当日负荷电量的 13% |
| `predicted_slack_sum_sq_pu` | 0 | 线性鲁棒模型内部完全可行，没有使用电压松弛 |
| `ac_outside_predicted_interval_node_periods` | 546（71%） | 鲁棒区间基本没有覆盖 AC 实测，说明主因是模型误差 |
| `max_nominal_prediction_error_sq_pu` | 0.035048 | 相当于约 0.017 p.u.（0.21 kV）的幅值误差 |
| `ac_realized_objective_same_weights` | 3.872 | 其中母线 18 时段 16 一项就贡献 `100 × 0.017648 = 1.765` |
| `ac_grid_q_limit_violation_periods` | 18 / 24 | 无功上限被系统性突破，最高 2309.6 kvar |

一句话结论：**数值上全部通过，物理上仍有 4 个过压节点时段和 18 个无功越限时段**；这些越限来自"线性模型 + 无损主网口径 + 软电压约束"的建模假设，而不是求解失败。

---

## 10. 只读核对脚本

下面的代码只读取文件、不写盘，可用于确认字段和形状：

```python
import json
from pathlib import Path
import numpy as np

run = Path('results/verified_run')
summary = json.loads((run / 'summary.json').read_text(encoding='utf-8'))
params = json.loads((run / 'parameters.json').read_text(encoding='utf-8'))

print(sorted(summary))
print(sorted(params))                                   # config / arrays / attributes / network
print({k: np.array(v).shape for k, v in params['arrays'].items()})

traj = np.load(run / 'trajectory.npz')
print(traj.files)
```

核对一致性时可以手算：`summary['active_loss_kwh']` 应等于 `periods.csv` 中 `loss_kw` 列之和（`dt = 1 h`）；`summary['predicted_slack_sum_sq_pu']` 应等于 `voltages.csv` 两个 slack 列之和。

---

## 11. 常见误读清单

1. **把 `predicted_slack_sum_sq_pu = 0` 当成"AC 电压合格"**。它只说明线性模型内部不需要松弛。
2. **把 `verification_maxima` 很小当成"物理结论可靠"**。它只说明公式实现正确。
3. **把 `all_executed_ac_converged` / `all_checks_passed` 当成重新计算的检查结果**。它们是字面量，其真实性由"执行流没有抛异常"保证。
4. **把 `initialization_full_horizon_objective` 与 `rolling_objective_sum` 相减或相比**。前者是 24 时段计划值，后者是 23 个单时段值之和。
5. **用 `periods[0].solver.objective` 代表初始化总目标**。它只是时段 0 的贡献。
6. **把电压平方当电压**。`1.1025` 对应 1.05 p.u.，`0.035` 的 V² 误差约等于 0.017 p.u. 的幅值误差。
7. **把优化器的 `grid_q` 与 AC 的 `grid_q` 对比**。前者无损、后者含损，二者本就不应相等。
8. **用当前 `config.py` 解释旧结果**。`parameters.json` 才是那次运行的参数快照。
9. **把 `predicted_lower/upper_sq` 当成统计置信区间**。它是盒式不确定集合下的最坏情况边界，不含概率含义。
10. **把 `dual_absolute_objective` 的微小非零值当成错误**。它受 MIP 间隙限制，在容差内即为等价。
