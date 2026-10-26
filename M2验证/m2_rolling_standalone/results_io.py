"""保存可追溯数据和中文验证说明，不参与建模或反馈控制。"""
import csv
import hashlib
import json
import platform
from importlib.metadata import version
from pathlib import Path
import numpy as np
from data import as_json


def write_json(path, value):
    Path(path).write_text(json.dumps(as_json(value), ensure_ascii=False, indent=2,
                                     allow_nan=False), encoding='utf-8')


def dispatch_arrays(actions):
    """把Dispatch列表转换为可直接保存的二维数组字典。"""
    return {name: np.array([getattr(action, name) for action in actions])
            for name in vars(actions[0])}


def save_results(directory, data, schedule, records, fingerprint, run_options):
    """保存本次运行，不读取任何旧工程结果。records每行对应一个已执行时段。"""
    directory = Path(directory)
    voltage = np.array([record['ac'].voltage_pu for record in records])
    nominal = np.array([record['nominal_sq'] for record in records])
    lower = np.array([record['lower_sq'] for record in records])
    upper = np.array([record['upper_sq'] for record in records])
    slack_upper = np.array([record['slack_upper'] for record in records])
    slack_lower = np.array([record['slack_lower'] for record in records])
    actions = [record['action'] for record in records]
    control = np.array([action.control for action in actions])
    ac_sq = np.array([record['ac'].voltage_sq for record in records])
    ac_upper = np.maximum(ac_sq - data.voltage_max_sq, 0)
    ac_lower = np.maximum(data.voltage_min_sq - ac_sq, 0)
    curtailed = np.zeros(len(records))
    for bus in data.pv_buses:
        curtailed += control[:, data.control_index['curt', bus]]
    loss = np.array([record['ac'].loss_p for record in records])
    grid_p = np.array([record['ac'].grid_p for record in records])
    grid_q = np.array([record['ac'].grid_q for record in records])
    check_maxima = {}
    for record in records:
        for name, value in record['checks'].items():
            check_maxima[name] = max(check_maxima.get(name, 0), value)
    voltage_tolerance = 1e-7
    summary = {
        'completed': True, 'executed_periods': len(records), 'rolling_solves': len(records) - 1,
        'all_executed_ac_converged': True, 'all_checks_passed': True,
        'parameter_fingerprint_before': fingerprint, 'parameter_fingerprint_after': data.fingerprint(),
        'initialization_full_horizon_objective': schedule.info['objective'],
        'rolling_objective_sum': sum(record['info']['objective'] for record in records[1:]),
        'ac_voltage_min_pu': float(voltage.min()), 'ac_voltage_max_pu': float(voltage.max()),
        'ac_lower_violation_node_periods': int((voltage < data.voltage_min - voltage_tolerance).sum()),
        'ac_upper_violation_node_periods': int((voltage > data.voltage_max + voltage_tolerance).sum()),
        'ac_voltage_violation_tolerance_pu': voltage_tolerance,
        'curtailment_kwh': float(curtailed.sum() * data.base_kw * data.dt),
        'active_loss_kwh': float(loss.sum() * data.base_kw * data.dt),
        'ac_realized_objective_same_weights': float(data.voltage_weight * (ac_upper.sum() + ac_lower.sum())
                                                  + data.curtailment_weight * curtailed.sum()),
        'predicted_slack_sum_sq_pu': float(slack_upper.sum() + slack_lower.sum()),
        'max_nominal_prediction_error_sq_pu': float(np.max(np.abs(ac_sq - nominal))),
        'ac_outside_predicted_interval_node_periods': int(((ac_sq < lower - 2e-7) | (ac_sq > upper + 2e-7)).sum()),
        'ac_grid_p_limit_violation_periods': int(((grid_p < data.grid_p_limits[0] - 2e-7) |
                                                (grid_p > data.grid_p_limits[1] + 2e-7)).sum()),
        'ac_grid_q_limit_violation_periods': int(((grid_q < data.grid_q_limits[0] - 2e-7) |
                                                (grid_q > data.grid_q_limits[1] + 2e-7)).sum()),
        'ac_grid_q_min_kvar': float(grid_q.min() * data.base_kw),
        'ac_grid_q_max_kvar': float(grid_q.max() * data.base_kw),
        'verification_maxima': check_maxima,
        'interpretation': 'MILP软约束可行、AC收敛、实际电压合格是不同判断。初始化目标为24时段总值；滚动目标和不含时段0。',
    }
    if data.fingerprint() != fingerprint:
        raise AssertionError('运行期间固定环境或不确定集合发生变化。')
    write_json(directory / 'summary.json', summary)
    write_json(directory / 'parameters.json', data.snapshot())
    write_json(directory / 'solver_and_checks.json',
               {'initialization': schedule.info, 'periods': [
                   {'period': record['period'], 'source': record['source'],
                    'solver': record['info'], 'checks': record['checks']} for record in records]})
    write_json(directory / 'manifest.json', {
        'python': platform.python_version(), 'libraries': {name: version(name) for name in ['numpy', 'scipy', 'gurobipy', 'PYPOWER']},
        'options': run_options, 'parameter_fingerprint': fingerprint,
        'source_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(Path(__file__).parent.glob('*.py'))},
    })
    arrays = dispatch_arrays(actions)
    arrays.update({'voltage_pu': voltage, 'measured_voltage_sq': ac_sq,
                   'predicted_nominal_sq': nominal, 'predicted_lower_sq': lower, 'predicted_upper_sq': upper,
                   'slack_upper': slack_upper, 'slack_lower': slack_lower,
                   'grid_p': grid_p, 'grid_q': grid_q,
                   'energy_before': np.array([record['energy_before'] for record in records]),
                   'p_injected': np.array([record['ac'].p_injected for record in records]),
                   'q_injected': np.array([record['ac'].q_injected for record in records]),
                   'branch_p_from': np.array([record['ac'].branch_p_from for record in records]),
                   'branch_q_from': np.array([record['ac'].branch_q_from for record in records]),
                   'branch_p_to': np.array([record['ac'].branch_p_to for record in records]),
                   'branch_q_to': np.array([record['ac'].branch_q_to for record in records])})
    if len(records) > 1:
        arrays.update({'feedback_voltage_sq': np.array([record['state_before'].measured_voltage_sq for record in records[1:]]),
                       'previous_control': np.array([record['state_before'].actual_control for record in records[1:]]),
                       'coefficient': np.array([record['step'].coefficient for record in records[1:]]),
                       'dual_upper': np.array([record['step'].dual_upper for record in records[1:]]),
                       'dual_lower': np.array([record['step'].dual_lower for record in records[1:]])})
    np.savez_compressed(directory / 'trajectory.npz', **arrays)
    init_arrays = dispatch_arrays(schedule.actions)
    init_arrays.update({'slack_upper': schedule.slack_upper, 'slack_lower': schedule.slack_lower,
                        'predicted_nominal_sq': schedule.predicted_nominal_sq,
                        'predicted_lower_sq': schedule.predicted_lower_sq,
                        'predicted_upper_sq': schedule.predicted_upper_sq})
    np.savez_compressed(directory / 'initial_static_plan.npz', **init_arrays)

    # CSV统一输出工程单位，使用UTF-8 BOM方便中文Windows Excel直接打开。
    with (directory / 'periods.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['period', 'source', 'objective', 'voltage_min_pu', 'voltage_max_pu',
                         'curtailment_kw', 'grid_p_kw', 'grid_q_kvar', 'loss_kw', 'solver_seconds'])
        for index, record in enumerate(records):
            writer.writerow([index, record['source'], record['info']['objective'], voltage[index].min(), voltage[index].max(),
                             curtailed[index] * data.base_kw, grid_p[index] * data.base_kw, grid_q[index] * data.base_kw,
                             loss[index] * data.base_kw, record['info']['solver_seconds']])
    with (directory / 'controls.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        labels = ['%s_bus%d_%s' % (kind, bus, 'kw' if kind in ('curt', 'pess') else 'kvar')
                  for kind, bus in data.controls]
        writer.writerow(['period'] + labels + ['energy_after_bus%d_kwh' % bus for bus in data.storage_buses])
        for index, action in enumerate(actions):
            writer.writerow([index] + (action.control * data.base_kw).tolist() + (action.energy_after * data.base_kw).tolist())
    with (directory / 'voltages.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['period', 'bus', 'actual_ac_voltage_pu', 'predicted_nominal_sq_pu',
                         'predicted_lower_sq_pu', 'predicted_upper_sq_pu', 'slack_upper_sq_pu', 'slack_lower_sq_pu'])
        for period in range(len(records)):
            for node, bus in enumerate(data.network.nonroot_buses):
                writer.writerow([period, bus, voltage[period, bus - 1], nominal[period, node], lower[period, node],
                                 upper[period, node], slack_upper[period, node], slack_lower[period, node]])

    report = '''# 本次独立M2运行验证

- 已执行时段：{executed_periods}；其中M2滚动求解：{rolling_solves}次。
- 全部已执行AC潮流收敛，设备、储能、鲁棒约束、对偶证书及独立潮流复核通过。
- AC电压范围：{ac_voltage_min_pu:.9f}～{ac_voltage_max_pu:.9f} p.u.。
- 实际欠压/过压节点时段数：{ac_lower_violation_node_periods} / {ac_upper_violation_node_periods}。
- 弃光电量：{curtailment_kwh:.9f} kWh；有功损耗电量：{active_loss_kwh:.6f} kWh。
- 含损耗AC主网有功/无功超限时段数：{ac_grid_p_limit_violation_periods} / {ac_grid_q_limit_violation_periods}。
- 全过程参数指纹一致；线路参数集合未在线更新。
- 自建24时段静态初始化目标值：{initialization_full_horizon_objective:.12f}。

## 如何理解“验证通过”

通过表示数值实现符合模型约束，且控制动作能在本场景完成AC执行。
电压是软约束，LinDistFlow也是近似模型，因此不保证AC电压始终在0.93～1.05 p.u.内。
不确定集合中的256个顶点只用于检查线性预测鲁棒性，不是对256个真实网络分别运行闭环。
主网限值在优化器中约束无损功率；AC潮流含线路损耗，实际超限需要单独报告。
当前不进行参数学习、预测误差修正或动作裁剪，也没有支路容量、日末SOC回归约束。

## 数据定位

- `parameters.json`：运行参数及矩阵快照。
- `manifest.json`：源码哈希、环境版本、运行选项。
- `solver_and_checks.json`：每步求解状态及残差；绝对值交叉验证以相同输入重新求解。
- `trajectory.npz`：真实执行轨迹、预测区间、反馈输入、对偶变量和储能状态。
- `initial_static_plan.npz`：完整初始化计划，仅第0行被执行，其余为核验存档。
- `periods.csv`：逐时段汇总；时段0的objective仅为该时段贡献，不是完整初始化总目标。
- `voltages.csv`：非根节点的真实幅值和预测平方电压，列名明确区分单位。
- `controls.csv`：实际执行的设备控制，功率已换算为kW/kvar，能量为kWh。
'''.format(**summary)
    (directory / '验证说明.md').write_text(report, encoding='utf-8')
    # 所有数值文件写完以后，再由独立脚本重新读取这些文件绘图。
    # 不把 records、data 或求解器对象交给绘图层；历史结果也可以单独重绘。
    save_result_figures(directory)
    return summary


def save_result_figures(directory):
    """可视化属于结果后处理；绘图失败不把已成功的模型运行标为失败。"""
    import sys
    import traceback
    try:
        from plot_results import generate_figures
        figure_dir = generate_figures(directory)
        print('可视化已保存：%s' % figure_dir, flush=True)
        return figure_dir
    except Exception:
        # 保留完整错误，用户修复依赖后可执行 plot_results.py 单独重绘。
        figure_dir = Path(directory) / 'figure'
        diagnostic = traceback.format_exc()
        try:
            figure_dir.mkdir(exist_ok=True)
            (figure_dir / 'plotting_error.txt').write_text(diagnostic, encoding='utf-8')
        except OSError:
            print(diagnostic, file=sys.stderr)
        print('数值结果已保存，但可视化失败。请查看 figure/plotting_error.txt；'
              '确认安装 requirements.txt 中的 matplotlib 后可单独重绘。',
              file=sys.stderr, flush=True)
        return None
