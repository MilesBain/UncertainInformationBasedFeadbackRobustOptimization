"""入口：M1初始化 -> 单步M2 -> AC执行/反馈 -> 下一时刻。"""
from __future__ import annotations

import argparse
import csv
import json
import platform
from dataclasses import asdict
from pathlib import Path
import sys
import numpy as np
import scipy
import gurobipy as gp

from data import ModelData, load_legacy, ROOT
from controller import initialize_m1, solve_step
from power_flow import ACPlant
from validation import verify_devices, verify_step, verify_power_flow, voltage_violations


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def maximum_checks(checks):
    keys = set().union(*(check.keys() for check in checks))
    return {k: max(c.get(k, 0) for c in checks) for k in sorted(keys)}


def record_period(data, period, dispatch, pf, step=None):
    row = {'period': period, 'method': 'M1_initialization' if step is None else 'M2',
           'ac_converged': True, **voltage_violations(data, pf.voltage_pu[1:])}
    row.update({'curtailment_kw': float(sum(dispatch.u[data.index['curt', b]] for b in data.pv)
                                      * data.power_base_kw),
                'grid_p_kw': pf.grid_p_pu * data.power_base_kw,
                'grid_q_kvar': pf.grid_q_pu * data.power_base_kw,
                'loss_p_kw': pf.loss_p_pu * data.power_base_kw,
                'ac_runtime_s': pf.runtime_s,
                'grid_p_limit_violation_kw': max(0, data.pm_lim[0] - pf.grid_p_pu,
                                                pf.grid_p_pu - data.pm_lim[1]) * data.power_base_kw,
                'grid_q_limit_violation_kvar': max(0, data.qm_lim[0] - pf.grid_q_pu,
                                                  pf.grid_q_pu - data.qm_lim[1]) * data.power_base_kw})
    if step is not None:
        row.update(step.info)
        row.update({'slack_plus_sum_sq': float(step.slack_plus.sum()),
                    'slack_minus_sum_sq': float(step.slack_minus.sum()),
                    'predicted_lower_min_sq': float(step.v_lower_sq.min()),
                    'predicted_upper_max_sq': float(step.v_upper_sq.max()),
                    'prediction_error_max_abs_sq': float(np.max(abs(pf.voltage_sq - step.v_nominal_sq))),
                    'ac_outside_linear_envelope_max_sq': float(max(0,
                        np.max(step.v_lower_sq - pf.voltage_sq), np.max(pf.voltage_sq - step.v_upper_sq)))})
    return row


def save_arrays(out, data, dispatches, flows, energies, steps, voltage_inputs, control_inputs):
    arrays = {'voltage_pu': np.array([f.voltage_pu for f in flows]),
              'voltage_measured_sq': np.array([f.voltage_sq for f in flows]),
              'energy_boundaries_pu_h': np.array(energies),
              'voltage_input_sq': np.array(voltage_inputs),
              'control_input_pu': np.array(control_inputs),
              'A': data.A, 'Bp': data.Bp, 'Bq': data.Bq, 'K': data.K,
              'theta_nom': data.theta_nom, 'theta_lower': data.theta_lo,
              'theta_upper': data.theta_hi, 'F': data.F, 'g': data.g,
              'p_ex_pu': data.p_ex, 'q_ex_pu': data.q_ex,
              'p_available_pu': data.p_available, 'p_load_pu': data.p_load, 'q_load_pu': data.q_load}
    for name in asdict(dispatches[0]):
        arrays[name] = np.array([getattr(a, name) for a in dispatches])
    for name in ('p_injected', 'q_injected', 'branch_p_from', 'branch_q_from',
                 'branch_p_to', 'branch_q_to'):
        arrays['ac_' + name] = np.array([getattr(pf, name) for pf in flows])
    for name in ('h', 'slack_plus', 'slack_minus', 'v_nominal_sq', 'v_lower_sq',
                 'v_upper_sq', 'lambda_plus', 'lambda_minus'):
        arrays[name] = np.array([getattr(s, name) for s in steps])
    np.savez_compressed(out / 'rolling_arrays.npz', **arrays)


def save_csv(out, data, records, dispatches, flows, energies, steps):
    fields = list(dict.fromkeys(key for row in records for key in row))
    with (out / 'period_summary.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    with (out / 'device_dispatch.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(['period', 'physical_bus', 'kind', 'value', 'unit'])
        for t, a in enumerate(dispatches):
            for j, (kind, bus) in enumerate(data.controls):
                writer.writerow([t, bus, kind, a.u[j] * data.power_base_kw,
                                 'kW' if kind in ('curt', 'pess') else 'kvar'])
            for j, bus in enumerate(data.ess):
                for kind in ('pch', 'pdis'):
                    writer.writerow([t, bus, kind, getattr(a, kind)[j] * data.power_base_kw, 'kW'])
                writer.writerow([t, bus, 'energy_before', energies[t][j] * data.power_base_kw, 'kWh'])
                writer.writerow([t, bus, 'energy_after', energies[t + 1][j] * data.power_base_kw, 'kWh'])
                writer.writerow([t, bus, 'soc_after', energies[t + 1][j] * data.power_base_kw /
                                 data.ess_params[bus]['e_capacity'], 'fraction'])
                for kind in ('zch', 'zdis'):
                    writer.writerow([t, bus, kind, getattr(a, kind)[j], 'binary'])
            for j, bus in enumerate(data.cb):
                writer.writerow([t, bus, 'gamma', a.gamma[j], 'step'])
    with (out / 'node_voltages.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(['period', 'physical_bus', 'ac_voltage_pu', 'ac_voltage_sq',
                         'nominal_predicted_sq', 'robust_lower_sq', 'robust_upper_sq', 's_plus', 's_minus'])
        for t, pf in enumerate(flows):
            for j in range(data.n):
                row = [t, j + 2, pf.voltage_pu[j + 1], pf.voltage_sq[j]]
                if t == 0:
                    row += ['', '', '', '', '']
                else:
                    s = steps[t - 1]
                    row += [s.v_nominal_sq[j], s.v_lower_sq[j], s.v_upper_sq[j],
                            s.slack_plus[j], s.slack_minus[j]]
                writer.writerow(row)


def write_report(out, summary):
    s = summary
    c = s['maximum_validation_residuals']
    rows = [
        '# M2 滚动优化可行性验证报告', '',
        '本报告只验证M2实现，不进行M1/M2性能对比。', '',
        '## 验证结论', '',
        f"- 完成 {s['m2_steps_completed']} 次单步M2优化和 {s['ac_power_flows_completed']} 次实际AC潮流（含初始化）。",
        f"- M2求解状态：全部 optimal；最大Gap：{s['max_mip_gap']:.3g}。",
        f"- 优化失败：{s['optimization_failures']}；实际AC潮流不收敛：{s['ac_failures']}。",
        f"- 对偶等式最大残差：{c.get('dual_equality_residual', 0):.3e}。",
        f"- 精确盒集鲁棒电压约束最大违反量：{max(c.get('robust_upper_violation', 0), c.get('robust_lower_violation', 0)):.3e} p.u.²。",
        f"- 设备约束最大违反量：{c['device_constraint_violation']:.3e}；储能能量平衡残差：{c['energy_balance_residual']:.3e} p.u.·h。",
        f"- PYPOWER与独立复电流前推回代AC电压最大差：{c['independent_ac_voltage_residual']:.3e} p.u.。",
        f"- 对偶式与独立绝对值式目标值核验：{len(s['absolute_formulation_checks'])} 个相同输入单步问题。",
        '', '## 原物理环境保持一致', '',
        'IEEE33全部32条支路与负荷、24小时曲线、10 MW光伏、SVG、电容器及节点18储能均来自未改动的M1源文件。',
        '真实阻抗采用M1原BRANCHES值；原代码没有另一份真实阻抗或AC网络数据。未知支路仍为5、12、16、29，r和x独立±30%，其余线路固定。',
        '运行前校验M1源文件SHA256。F/g只初始化一次，并逐步记录相同哈希；根节点电压固定1 p.u.。',
        '', '## 实际运行结果（全序列含初始时段）', '',
        f"- 实际非根节点电压范围：[{s['actual_voltage_min_pu']:.8f}, {s['actual_voltage_max_pu']:.8f}] p.u.；允许范围[0.93, 1.05]。",
        f"- 实际下/上越限节点时段数：{s['undervoltage_node_periods']} / {s['overvoltage_node_periods']}；发生越限的时段数：{s['voltage_violation_periods']}。",
        f"- 最大实际下/上越限：{s['max_undervoltage_pu']:.8f} / {s['max_overvoltage_pu']:.8f} p.u.。",
        f"- 总弃光电量：{s['curtailment_kwh']:.6f} kWh；可用光伏电量：{s['pv_available_kwh']:.2f} kWh。",
        f"- M2电压松弛量总和：{s['m2_slack_sum_sq']:.10f} p.u.²；M2目标值之和：{s['m2_objective_sum']:.10f}。",
        f"- 最大名义电压预测误差：{s['max_prediction_error_sq']:.8f} p.u.²。",
        f"- 单步Gurobi求解时间均值/最大值：{s['mean_solver_runtime_s']:.5f} / {s['max_solver_runtime_s']:.5f} s。",
        f"- 含建模的单步耗时均值：{s['mean_build_solve_s']:.5f} s。",
        f"- 实际主网有功/无功限值最大超出：{s['max_ac_grid_p_violation_kw']:.6f} kW / {s['max_ac_grid_q_violation_kvar']:.6f} kvar。",
        '', '## 时序与验收边界', '',
        '时段0采用原M1求解动作并执行AC潮流；E_boundary[0]为原初始能量，时段0充放电后得到E_boundary[1]。',
        '对k=1..23，使用时段k-1的AC电压平方及已执行动作，优化k的设备控制，使用E_boundary[k]约束执行后的E_boundary[k+1]。所有24次动作只计量一次。',
        '每步独立穷举8个不确定参数的全部256个顶点，检查盒集极值、对偶等式/不等式、设备约束和能量平衡；实际AC还检查注入映射、节点功率平衡与独立求解器一致性。',
        '软约束模型可行与实际电压不越限是两个不同结论。LinDistFlow鲁棒包络约束近似递推电压，不能作为非线性AC电压的严格保证。',
        '主网原P/Q交换范围保留为无损耗聚合注入约束；真实AC交换含网损，报告实际越界而不擅自扩大原限值。没有支路容量约束、在线参数修正、动作惩罚、日末SOC回归或视在容量新增约束。',
        '', '## 文件', '',
        '- summary.json：汇总、参数哈希和校验残差。',
        '- period_summary.csv：逐时段求解信息、电压越限、弃光和主网交换。',
        '- device_dispatch.csv：逐设备动作和储能能量，使用kW/kvar/kWh。',
        '- node_voltages.csv：逐节点真实电压、鲁棒包络、松弛量。',
        '- rolling_arrays.npz：完整数值数组、矩阵、对偶变量及反馈输入，可复核递推。',
        '- validation_details.json：各步独立检查与等价形式验算。',
        '', '## 运行环境', '',
        f"Python {s['environment']['python']}; NumPy {s['environment']['numpy']}; SciPy {s['environment']['scipy']}; Gurobi {s['environment']['gurobi']}; PYPOWER 5.1.19。", '',
    ]
    (out / '验证报告.md').write_text('\n'.join(rows), encoding='utf-8')


def run(output_dir, steps=23, log=False, time_limit=120, extra_checks=True):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = ModelData(load_legacy())
    if not 1 <= steps < data.periods:
        raise ValueError('steps must be between 1 and %d' % (data.periods - 1))
    plant = ACPlant(data.legacy)
    plant.check_box_membership(data)
    metadata = data.metadata()
    write_json(out / 'configuration.json', metadata)
    digest = data.uncertainty_digest()
    dispatches, flows, energies, results, records = [], [], [data.initial_energy.copy()], [], []
    checks, absolute_checks, voltage_inputs, control_inputs = [], [], [], []
    stage = 'M1 initialization'
    try:
        action, m1_info = initialize_m1(data, log, time_limit)
        checks.append(verify_devices(data, 0, action, energies[0]))
        stage = 'AC initialization'
        flow = plant.run(data, 0, action)
        checks.append(verify_power_flow(data, plant, flow))
        dispatches.append(action)
        flows.append(flow)
        energies.append(action.energy_after.copy())
        records.append(record_period(data, 0, action, flow))
        print('Initialized from M1; initial AC range [%.6f, %.6f] p.u.' %
              (flow.voltage_pu.min(), flow.voltage_pu.max()), flush=True)
        for t in range(steps):
            stage = 'M2 optimization at period %d' % (t + 1)
            # 唯一反馈来源：已成功执行的AC潮流，不使用预测电压。
            measured = flows[-1].voltage_sq.copy()
            actual = dispatches[-1].u.copy()
            energy_before = energies[-1].copy()
            result = solve_step(data, t, measured, actual, energy_before, 'dual', log,
                                time_limit, out / 'diagnostics')
            checks.append(verify_step(data, t, measured, actual, energy_before, result))
            if extra_checks and t in {0, 9, 12, 18, 22}:
                stage = 'independent absolute formulation at period %d' % (t + 1)
                alt = solve_step(data, t, measured, actual, energy_before, 'absolute', False, time_limit)
                checks.append(verify_step(data, t, measured, actual, energy_before, alt))
                difference = abs(result.info['objective'] - alt.info['objective'])
                if difference > 2e-7 * (1 + abs(result.info['objective'])):
                    raise AssertionError('Dual and absolute formulations disagree on objective.')
                absolute_checks.append({'period': t + 1, 'dual_objective': result.info['objective'],
                                        'absolute_objective': alt.info['objective'],
                                        'objective_difference': difference})
            stage = 'AC execution at period %d' % (t + 1)
            next_flow = plant.run(data, t + 1, result.dispatch)
            checks.append(verify_power_flow(data, plant, next_flow))
            if data.uncertainty_digest() != digest:
                raise AssertionError('Fixed U0 was modified.')
            # 所有验证及AC成功后提交状态，绝不吞掉失败后沿用预测值。
            voltage_inputs.append(measured)
            control_inputs.append(actual)
            dispatches.append(result.dispatch)
            flows.append(next_flow)
            energies.append(result.dispatch.energy_after.copy())
            results.append(result)
            records.append(record_period(data, t + 1, result.dispatch, next_flow, result))
            write_json(out / 'progress.json', {'status': 'running', 'completed_period': t + 1,
                                               'completed_m2_steps': len(results)})
            print('Period %02d optimal; obj=%.8f; AC=[%.6f, %.6f]; gap=%.1e' %
                  (t + 1, result.info['objective'], next_flow.voltage_pu.min(),
                   next_flow.voltage_pu.max(), result.info['gap']), flush=True)
    except Exception as exc:
        write_json(out / 'failure.json', {'status': 'failed', 'stage': stage, 'error': str(exc),
                                         'completed_m2_steps': len(results),
                                         'optimization_failures': int('optimization' in stage or stage.startswith('M1')),
                                         'ac_failures': int(stage.startswith('AC'))})
        if dispatches:
            save_arrays(out, data, dispatches, flows, energies, results, voltage_inputs, control_inputs)
        raise
    all_voltages = np.array([pf.voltage_pu[1:] for pf in flows])
    residuals = maximum_checks(checks)
    feedback_error = float(np.max(abs(np.array(voltage_inputs) - all_voltages[:-1] ** 2)))
    if feedback_error != 0:
        raise AssertionError('Measured AC feedback is not used verbatim.')
    residuals['feedback_identity_residual'] = feedback_error
    m2_records = records[1:]
    summary = {
        'status': 'passed', 'm2_steps_completed': len(results), 'ac_power_flows_completed': len(flows),
        'full_24_period_run': steps == 23, 'optimization_failures': 0, 'ac_failures': 0,
        'source_sha256': metadata['source_sha256'], 'uncertainty_sha256': digest,
        'max_mip_gap': max(s.info['gap'] for s in results),
        'actual_voltage_min_pu': float(all_voltages.min()), 'actual_voltage_max_pu': float(all_voltages.max()),
        'undervoltage_node_periods': sum(r['undervoltage_nodes'] for r in records),
        'overvoltage_node_periods': sum(r['overvoltage_nodes'] for r in records),
        'voltage_violation_periods': sum(r['undervoltage_nodes'] + r['overvoltage_nodes'] > 0 for r in records),
        'max_undervoltage_pu': max(r['max_undervoltage_pu'] for r in records),
        'max_overvoltage_pu': max(r['max_overvoltage_pu'] for r in records),
        'curtailment_kwh': sum(r['curtailment_kw'] for r in records) * data.dt,
        'pv_available_kwh': float(data.p_available[:steps + 1].sum() * data.power_base_kw * data.dt),
        'm2_slack_sum_sq': sum(r['slack_plus_sum_sq'] + r['slack_minus_sum_sq'] for r in m2_records),
        'm2_objective_sum': sum(r['objective'] for r in m2_records),
        'max_prediction_error_sq': max(r['prediction_error_max_abs_sq'] for r in m2_records),
        'max_ac_outside_linear_envelope_sq': max(r['ac_outside_linear_envelope_max_sq'] for r in m2_records),
        'mean_solver_runtime_s': float(np.mean([r['runtime_s'] for r in m2_records])),
        'max_solver_runtime_s': max(r['runtime_s'] for r in m2_records),
        'mean_build_solve_s': float(np.mean([r['build_and_solve_s'] for r in m2_records])),
        'max_ac_grid_p_violation_kw': max(r['grid_p_limit_violation_kw'] for r in records),
        'max_ac_grid_q_violation_kvar': max(r['grid_q_limit_violation_kvar'] for r in records),
        'maximum_validation_residuals': residuals, 'absolute_formulation_checks': absolute_checks,
        'm1_initialization': m1_info,
        'storage_initial_energy_kwh': (energies[0] * data.power_base_kw).tolist(),
        'storage_final_energy_kwh': (energies[-1] * data.power_base_kw).tolist(),
        'environment': {'python': platform.python_version(), 'numpy': np.__version__,
                        'scipy': scipy.__version__, 'gurobi': '.'.join(map(str, gp.gurobi.version())),
                        'python_executable': sys.executable, 'pypower': '5.1.19'},
    }
    save_arrays(out, data, dispatches, flows, energies, results, voltage_inputs, control_inputs)
    save_csv(out, data, records, dispatches, flows, energies, results)
    write_json(out / 'summary.json', summary)
    write_json(out / 'validation_details.json', {'residual_checks': checks, 'absolute_checks': absolute_checks})
    write_json(out / 'progress.json', {'status': 'passed', 'completed_period': steps, 'completed_m2_steps': steps})
    # 清除本程序自己上次在同一输出目录留下的过期失败标记。
    failure = out / 'failure.json'
    if failure.exists():
        failure.unlink()
    write_report(out, summary)
    print('Validation PASSED; results: %s' % out.resolve(), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'results')
    parser.add_argument('--steps', type=int, default=23, help='M2 transitions; 1=single step, 23=full series')
    parser.add_argument('--solver-log', action='store_true')
    parser.add_argument('--time-limit', type=float, default=120)
    parser.add_argument('--skip-absolute-checks', action='store_true')
    args = parser.parse_args()
    run(args.output, args.steps, args.solver_log, args.time_limit, not args.skip_absolute_checks)


if __name__ == '__main__':
    main()
