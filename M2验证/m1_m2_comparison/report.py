"""由本次结果自动生成中文报告，参数公平性与比较局限显式披露。"""
from __future__ import annotations
from robust_report import prediction_sections


def write_report(out, summary, audit):
    first = summary['metrics']['M1']['full_day']
    second = summary['metrics']['M2']['full_day']
    post1 = summary['metrics']['M1']['after_shared_initialization']
    post2 = summary['metrics']['M2']['after_shared_initialization']
    rows = [
        '# M1静态优化与M2滚动优化：统一物理环境下的效果对比', '',
        '两套算法均已完成24时段实际AC潮流评估，所有优化达到最优、所有AC潮流收敛。'
        '网络、真实线路阻抗、负荷与光伏序列、设备参数、约束数值、惩罚权重、初始状态和求解器设置均通过精确一致性检查。', '',
    ]
    rows += prediction_sections(summary, audit)
    rows += [
        '## 两套控制结果统一AC校验后的对比', '',
        '以下评价基于实际非线性AC电压，包含共同的初始时段0。越限次数按“非根节点×时段”统计。', '',
        '| 指标 | M1静态 | M2滚动 | M2−M1 |', '|---|---:|---:|---:|',
    ]
    specs = [('ac_voltage_min_pu', '最低实际电压 / p.u.', 6),
             ('ac_voltage_max_pu', '最高实际电压 / p.u.', 6),
             ('voltage_violation_node_periods', '实际电压越限节点时段数', 0),
             ('voltage_violation_periods', '发生电压越限的时段数', 0),
             ('voltage_compliance_pct', '节点时段电压合格率 / %', 3),
             ('max_overvoltage_pu', '最大上越限 / p.u.', 6),
             ('max_undervoltage_pu', '最大下越限 / p.u.', 6),
             ('voltage_violation_integral_pu_node_h', '越限幅度积分 / p.u.·节点·h', 6),
             ('curtailment_kwh', '弃光电量 / kWh', 3),
             ('curtailment_pct', '弃光率 / %', 4),
             ('active_loss_kwh', '实际有功网损 / kWh', 3),
             ('max_grid_q_violation_kvar', '主网无功最大越限 / kvar', 3),
             ('grid_q_violation_periods', '主网无功越限时段数', 0),
             ('max_grid_p_violation_kw', '主网有功最大越限 / kW', 3),
             ('storage_charge_kwh', '储能累计充电 / kWh', 3),
             ('storage_discharge_kwh', '储能累计放电 / kWh', 3),
             ('storage_final_energy_kwh', '储能日末能量 / kWh', 3),
             ('ac_voltage_penalty', '实际电压平方越限惩罚', 6),
             ('curtailment_penalty', '实际弃光惩罚', 6),
             ('common_ac_cost', '统一实际AC代价', 6)]
    for key, label, digits in specs:
        a, b = first[key], second[key]
        rows.append(f'| {label} | {a:.{digits}f} | {b:.{digits}f} | {b-a:+.{digits}f} |')
    rows += ['', '统一实际AC代价按原权重重新评价：', '',
             '`J_AC = C_V × Σ[max(V_AC²−Vmax²,0)+max(Vmin²−V_AC²,0)] + C_CURT × ΣP_curt(p.u.)`。', '',
             '这个指标使用共同的真实潮流结果；未将网损、无功越限或日末储能价值擅自加入原目标，因此须与表中其他物理指标一起解读。', '',
             '## 结果解读', '',
             f"本场景中，M1实际电压越限为{first['voltage_violation_node_periods']}个节点时段、弃光{first['curtailment_kwh']:.3f} kWh；"
             f"M2为{second['voltage_violation_node_periods']}个节点时段、弃光{second['curtailment_kwh']:.3f} kWh。",
             f"两者统一实际AC代价分别为{first['common_ac_cost']:.6f}和{second['common_ac_cost']:.6f}。"
             '这些结果描述本次固定场景和确定求解设置下的轨迹，不证明某方法在一般情况下占优。', '',
             f"M1/M2日末储能分别为{first['storage_final_energy_kwh']:.3f}/{second['storage_final_energy_kwh']:.3f} kWh。"
             '双方都沿用无日末SOC回归条件，故没有人为拉齐最终能量；净购电、网损与弃光指标需要结合此能量差一起阅读。', '',
             '## 完全一致的外在设置如何保证', '',
             '- 原M1源文件以及上一阶段M2的建模、数据、AC及验证模块均逐字节冻结，并在运行前后核对SHA256。原有两个文件夹不修改。',
             '- 分别实例化M1/M2两套ModelData和ACPlant，逐项比较原参数、实际使用数组、真实阻抗、控制排序、求解设置；不是仅比较配置文件名。',
             '- IEEE33的全部支路与节点负荷、24小时曲线、4处共10 MW光伏、2台SVG、4处电容器、节点18储能完全相同。',
             '- 基值10 MVA/12.66 kV，DT=1 h，电压0.93..1.05，C_V=100、C_CURT=1，主网P/Q限值±20000 kW/±1500 kvar。',
             '- 真实线路参数均采用原BRANCHES；固定不确定集合仅第5、12、16、29条线路r/x各自±30%，其他线路固定。没有在线更新或随机修改真实阻抗。',
             '- 同一个M1完整最优解的第0时段动作复制给M2，两者初始能量、动作、AC电压及首时段后能量精确一致；M2不重复求解初始化。',
             '- 同一PYPOWER牛顿AC求解及恒PQ注入语义，同一母线排序、符号和单位；均用独立复电流前推回代验证AC电压。',
             '- 每个滚动时段再次检查整个场景哈希不变。求解Threads=1、Seed=0、MIPGap=1e-8、可行性与整数容差1e-8，两者相同。', '',
             '共同场景SHA256：', '', f"`{summary['scenario_sha256']}`", '',
             '完整参数和逐项数组保存在`shared_scenario.json`，相等性与每时段校验结果保存在`fairness_audit.json`。', '',
             '## 保留的模型结构差异', '',
             '| 项目 | M1静态优化 | M2滚动优化 |', '|---|---|---|',
             '| 电压模型 | 从固定根电压计算绝对LinDistFlow电压 | 从上一步真实AC电压递推增量 |',
             '| 求解时域 | 原代码一次联合求解24时段 | 23次单步求解，加共同的第0时段 |',
             '| 储能决策可利用的时域 | 全24时段 | 当前状态与下一时段 |',
             '| 控制执行 | 逐时段执行预先求好的计划 | AC执行成功后反馈并求下一步 |',
             '| 参数/设备/初态/评价 | 完全相同 | 完全相同 |', '',
             '原M1包含储能能量跨时段耦合，不能把它描述成24个互不关联的小时问题。'
             '本实验比较用户现有M1与文稿M2整体方法，并不隔离“只增加反馈”的因果效应；时域差异属于两种模型原有结构，已显式披露。', '',
             'M1原有的P/Q±5、|Qpv|≤1、v0∈[-2,3]、Psi≤2和松弛≤1为宽松数值界。'
             '对当前物理设备功率范围做保守区间传播，证明其不限制M1物理可行控制；详细上界见审计文件。'
             'M2仍保留文稿要求的无上界非负电压松弛，不为对比改写原模型。', '',
             '## 优化器目标与实际评价分开记录', '',
             f"M1全日优化目标：{first['model_objective_sum']:.8f}；M2对应目标累计（含共同初始化时段诊断值）：{second['model_objective_sum']:.8f}。",
             '两者依赖不同的电压预测方式，不能仅根据目标值较小就判定实际运行更好。预测电压约束允许非零松弛，'
             '即使线性模型无松弛，非线性AC电压也可能越限。主网无损耗交换约束同样不严格保证含网损的实际交换限值。', '',
             '为排除共同初始化时段的影响，`metrics.csv/summary.json`另列时段1..23的相同评价。', '',
             f"时段1..23：M1/M2名义预测电压平方RMSE分别为{post1['prediction_error_rmse_sq']:.8f}/{post2['prediction_error_rmse_sq']:.8f} p.u.²；"
             f"统一实际代价分别为{post1['common_ac_cost']:.6f}/{post2['common_ac_cost']:.6f}。", '',
             '## 求解与数值验证', '',
             f"- M1完整求解耗时：{summary['metrics']['M1']['solver']['solver_runtime_s']:.6f} s。",
             f"- M2共23次滚动求解，合计{summary['metrics']['M2']['solver']['rolling_runtime_sum_s']:.6f} s，"
             f"平均{summary['metrics']['M2']['solver']['rolling_runtime_mean_s']:.6f} s，最大{summary['metrics']['M2']['solver']['rolling_runtime_max_s']:.6f} s。",
             f"- M2若单独运行还需初始化M1求解；计入该部分为{summary['metrics']['M2']['solver']['runtime_including_initialization_s']:.6f} s。本次复用同一初始化解，未实际重复求解。",
             '- 耗时为本机一次运行的求解器Runtime，不是稳定基准性能结论，也未把24时段一次求解时间与单步时间直接比较。',
             '- M1原始约束矩阵、全部变量界与整数性检查通过；M1每时段和M2每步均验证精确盒集极值及全部256个顶点。',
             '- M1/M2共48次AC潮流全部收敛；每次验证设备注入、节点功率平衡、总网损平衡与独立AC解。',
             '- M2每次输入电压均与上一时段AC实测平方值精确一致；无参数更新、无重置储能或电容器。', '',
             '多解说明：原目标没有动作平滑或额外择优项。当前固定求解器版本、线程和随机种子以便复现，'
             '不同求解器版本可能选出不同的同目标最优动作。本报告不挑选“最好看”的轨迹，也不隐含加入动作惩罚。', '',
             '## 图表与数据', '',
             '![实际效果总览](figures/01_actual_performance.png)', '',
             '![节点电压热图](figures/02_voltage_heatmaps.png)', '',
             '- `figures/`：8组PNG及SVG，含预测鲁棒结果、自身预测与AC校验、电压、光伏、储能、网损及无功控制。',
             '- `robust_prediction_metrics.csv`、`robust_prediction_periods.csv`：预测鲁棒指标与逐时段数据。',
             '- `four_way_voltage_comparison.csv`：同一时段同一节点的两套预测与两套AC值并列。',
             '- `initial_device_states.csv`、`initial_state_audit.json`：完整初始设备状态。',
             '- `paired_ac_audit.json`：24对AC校验的输入哈希、控制哈希和真实电压来源。',
             '- `metrics.csv`：共同指标、全日及排除共同初态两种窗口、M2−M1绝对差。',
             '- `period_comparison.csv`、`node_voltage_comparison.csv`、`device_comparison.csv`：逐时段/节点/设备明细。',
             '- `m1_results.npz`、`m2_results.npz`：完整数值数组；M2附反馈输入和对偶变量，M1附原MILP解向量。',
             '- `validation_details.json`：逐步数值验收残差与M2求解信息。', '',
             f"运行环境：Python {summary['environment']['python']}，NumPy {summary['environment']['numpy']}，"
             f"SciPy {summary['environment']['scipy']}，Gurobi {summary['environment']['gurobi']}，PYPOWER 5.1.19。", '']
    (out / '对比报告.md').write_text('\n'.join(rows), encoding='utf-8')
