"""预测鲁棒结果的独立报告段落，避免与AC执行结果混用。"""


def prediction_sections(summary, audit):
    m1 = summary['robust_prediction_metrics']['M1']['comparison_periods_1_to_23']
    m2 = summary['robust_prediction_metrics']['M2']['comparison_periods_1_to_23']
    weights = audit['objective_coefficients']
    rows = ['## 本次三项要求的对应检查', '',
            f"1. **条件一致。** M1/M2均使用C_V={weights['C_V_M1']:.0f}、C_CURT={weights['C_CURT_M1']:.0f}，"
            '原M1实际目标向量的全部变量系数已逐列核验。其余物理参数和实际使用数组逐项比较。',
            '2. **双边AC校验。** 每个时段分别将M1、M2控制结果写入相同环境的AC模型，共24对、48次runpf。'
            'M1的实际电压也是AC重新计算值，不使用其静态预测电压替代。',
            '3. **双边预测对比。** 分别保存并比较两种优化器自己的名义电压、鲁棒上下界、松弛和目标，再与各自AC结果核对。', '',
            '| 结果类别 | M1 | M2 |', '|---|---|---|',
            '| 优化器预测 | 1 + H_static·theta 的盒集逐节点极值 | 上一步AC电压平方 + H_delta·theta 的盒集逐节点极值 |',
            '| 控制执行校验 | AC(theta*, 外部输入, M1控制) | AC(theta*, 外部输入, M2控制) |',
            '| 下一步优化反馈 | 不反馈AC电压，执行已求好的静态计划 | 使用本方法上一步AC电压与已执行动作 |', '',
            'M2使用AC量测作为起点，并不意味着其下一时段预测是“真实值”。'
            '两种方法的预测都使用LinDistFlow；只有各自控制执行后的AC潮流输出作为本仿真的真实校验值。', '',
            '## 各设备初始状态', '',
            '共同起点来自同一次M1完整求解的第0时段，逐项核对完整Dispatch：'
            '`u、pch、pdis、zch、zdis、gamma、energy_after`，并检查执行前E/SOC、'
            '执行后E/SOC、全部母线复电压完全相同。没有只比较SOC而遗漏无功或档位。', '',
            '`initial_device_states.csv`逐设备列出M1值、M2值及差值（全部为0）；'
            '`initial_state_audit.json`保留完整原始状态和两边哈希。'
            '初始设备状态一致；后续状态由各自控制动作自然演化，不强制拉齐。', '',
            '## 预测鲁棒结果对比（目标时段1–23）', '',
            'M2在时段0没有独立预测，该行来自共同M1初始化，不能算作M2预测成果。'
            '下表在两边都使用相同的23个目标时段、32个非根节点。含共同初态的全日统计另存CSV。', '',
            '| 预测指标 | M1预测 | M2预测 |', '|---|---:|---:|']
    specs = [('robust_lower_min_pu', '最坏下界最小值 / p.u.', 6),
             ('robust_upper_max_pu', '最坏上界最大值 / p.u.', 6),
             ('mean_interval_width_sq', '平均鲁棒区间宽度 / p.u.²', 8),
             ('max_interval_width_sq', '最大鲁棒区间宽度 / p.u.²', 8),
             ('robust_hard_limit_violation_node_periods', '预测包络越过原电压限值的节点时段数', 0),
             ('slack_plus_sum_sq', '上限松弛总量 / p.u.²', 8),
             ('slack_minus_sum_sq', '下限松弛总量 / p.u.²', 8),
             ('predicted_voltage_penalty', '预测电压越限惩罚', 8),
             ('predicted_curtailment_penalty', '预测弃光惩罚', 8),
             ('predicted_robust_objective_sum', '预测鲁棒目标累计值', 8),
             ('max_relaxed_robust_constraint_violation_sq', '考虑松弛后的鲁棒约束最大残差 / p.u.²', 10)]
    for key, label, digits in specs:
        a, b = m1[key], m2[key]
        left = '不可转换（平方值为负）' if a is None else f'{a:.{digits}f}'
        right = '不可转换（平方值为负）' if b is None else f'{b:.{digits}f}'
        rows.append(f'| {label} | {left} | {right} |')
    rows += ['', '这里的预测最坏电压按相同盒集精确计算；每个节点的最坏参数可能不同，'
             '所有节点极值不一定由同一组参数同时达到。M1约束绝对电压，M2约束量测起点下的增量预测，'
             '所以即使外在参数相同，预测区间宽度和目标值也允许不同。', '',
             '![两种方法的预测鲁棒结果](figures/07_robust_prediction_comparison.png)', '',
             '## 各自预测包络与各自AC校验的对应关系（时段1–23）', '',
             '| 指标 | M1预测 vs AC(M1控制) | M2预测 vs AC(M2控制) |', '|---|---:|---:|']
    for key, label, digits in [
        ('ac_outside_prediction_interval_node_periods', 'AC落在自身预测区间之外的节点时段数', 0),
        ('ac_prediction_interval_coverage_pct', '自身预测区间对AC结果的覆盖率 / %', 3),
        ('max_ac_prediction_interval_exceedance_sq', 'AC偏离自身预测区间的最大幅度 / p.u.²', 8),
        ('nominal_vs_ac_rmse_sq', '名义预测对AC结果的RMSE / p.u.²', 8)]:
        rows.append(f'| {label} | {m1[key]:.{digits}f} | {m2[key]:.{digits}f} |')
    rows += ['', '覆盖率是本次非线性AC结果落入线性鲁棒包络的经验比例，不是概率置信度，'
             '也不同于电压合格率。落出预测区间时实际电压仍可能合格；反之不能由预测可行推断AC必然合格。', '',
             '![逐节点自身预测包络与自身AC校验](figures/08_predictions_and_ac.png)', '',
             '`four_way_voltage_comparison.csv`在同一行并列给出同一时段、同一节点的'
             'M1预测上下界/名义值、AC(M1控制)、M2预测上下界/名义值、AC(M2控制)，便于逐点复核。', '']
    return rows
