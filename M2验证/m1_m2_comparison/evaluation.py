"""同一AC结果上的统一评价与逐时段导出，明确区分预测目标和实际代价。"""
from __future__ import annotations

import csv
import numpy as np

VOLTAGE_COUNT_TOL_PU = 1e-7
POWER_COUNT_TOL_KW = 1e-3


def arrays_from_trace(data, actions, flows, predictions, energies):
    arrays = {
        'voltage_pu': np.array([p.voltage_pu for p in flows]),
        'voltage_sq': np.array([p.voltage_sq for p in flows]),
        'u': np.array([a.u for a in actions]),
        'pch': np.array([a.pch for a in actions]),
        'pdis': np.array([a.pdis for a in actions]),
        'zch': np.array([a.zch for a in actions]),
        'zdis': np.array([a.zdis for a in actions]),
        'gamma': np.array([a.gamma for a in actions]),
        'energy_boundaries_pu_h': np.array(energies),
        'grid_p_kw': np.array([p.grid_p_pu for p in flows]) * data.power_base_kw,
        'grid_q_kvar': np.array([p.grid_q_pu for p in flows]) * data.power_base_kw,
        'loss_p_kw': np.array([p.loss_p_pu for p in flows]) * data.power_base_kw,
        'loss_q_kvar': np.array([p.loss_q_pu for p in flows]) * data.power_base_kw,
        'ac_runtime_s': np.array([p.runtime_s for p in flows]),
        'nominal_sq': np.array([p['nominal_sq'] for p in predictions]),
        'lower_sq': np.array([p['lower_sq'] for p in predictions]),
        'upper_sq': np.array([p['upper_sq'] for p in predictions]),
        'slack_plus': np.array([p['sp'] for p in predictions]),
        'slack_minus': np.array([p['sm'] for p in predictions]),
        'model_objective': np.array([p['objective'] for p in predictions]),
    }
    for kind in ('branch_p_from', 'branch_q_from', 'branch_p_to', 'branch_q_to', 'p_injected', 'q_injected'):
        arrays['ac_' + kind] = np.array([getattr(p, kind) for p in flows])
    arrays['curtailment_kw'] = arrays['u'][:, [data.index['curt', b] for b in data.pv]].sum(axis=1) * data.power_base_kw
    arrays['pv_available_kw'] = data.p_available.sum(axis=1) * data.power_base_kw
    arrays['pv_actual_kw'] = arrays['pv_available_kw'] - arrays['curtailment_kw']
    return arrays


def evaluate(data, arrays, start=0):
    sl = slice(start, data.periods)
    v = arrays['voltage_pu'][sl, 1:]
    vsq = arrays['voltage_sq'][sl]
    under, over = np.maximum(data.vmin - v, 0), np.maximum(v - data.vmax, 0)
    under_sq, over_sq = np.maximum(data.vlo - vsq, 0), np.maximum(vsq - data.vhi, 0)
    counts = (under > VOLTAGE_COUNT_TOL_PU) | (over > VOLTAGE_COUNT_TOL_PU)
    curtail = float(arrays['curtailment_kw'][sl].sum() * data.dt)
    available = float(arrays['pv_available_kw'][sl].sum() * data.dt)
    power_base = data.power_base_kw
    p, q = arrays['grid_p_kw'][sl], arrays['grid_q_kvar'][sl]
    p_exceed = np.maximum(np.maximum(p - data.pm_lim[1] * power_base, data.pm_lim[0] * power_base - p), 0)
    q_exceed = np.maximum(np.maximum(q - data.qm_lim[1] * power_base, data.qm_lim[0] * power_base - q), 0)
    squared_penalty = float(data.cv * (under_sq + over_sq).sum())
    curtail_penalty = float(data.ccurt * arrays['curtailment_kw'][sl].sum() / power_base)
    error_sq = vsq - arrays['nominal_sq'][sl]
    return {
        'periods': data.periods - start, 'first_period': start, 'nonroot_nodes': data.n,
        'ac_voltage_min_pu': float(v.min()), 'ac_voltage_max_pu': float(v.max()),
        'undervoltage_node_periods': int((under > VOLTAGE_COUNT_TOL_PU).sum()),
        'overvoltage_node_periods': int((over > VOLTAGE_COUNT_TOL_PU).sum()),
        'voltage_violation_node_periods': int(counts.sum()),
        'voltage_violation_periods': int(counts.any(axis=1).sum()),
        'voltage_compliance_pct': float(100 * (1 - counts.mean())),
        'max_undervoltage_pu': float(under.max()), 'max_overvoltage_pu': float(over.max()),
        'voltage_violation_integral_pu_node_h': float((under + over).sum() * data.dt),
        'curtailment_kwh': curtail, 'pv_available_kwh': available,
        'curtailment_pct': curtail / available * 100 if available else 0.0,
        'active_loss_kwh': float(arrays['loss_p_kw'][sl].sum() * data.dt),
        'grid_import_kwh': float(np.maximum(p, 0).sum() * data.dt),
        'grid_export_kwh': float(np.maximum(-p, 0).sum() * data.dt),
        'max_grid_p_violation_kw': float(p_exceed.max()),
        'max_grid_q_violation_kvar': float(q_exceed.max()),
        'grid_q_violation_periods': int((q_exceed > POWER_COUNT_TOL_KW).sum()),
        'grid_p_violation_periods': int((p_exceed > POWER_COUNT_TOL_KW).sum()),
        'storage_charge_kwh': float(arrays['pch'][sl].sum() * power_base * data.dt),
        'storage_discharge_kwh': float(arrays['pdis'][sl].sum() * power_base * data.dt),
        'storage_start_energy_kwh': float(arrays['energy_boundaries_pu_h'][start].sum() * power_base),
        'storage_final_energy_kwh': float(arrays['energy_boundaries_pu_h'][-1].sum() * power_base),
        'ac_voltage_penalty': squared_penalty, 'curtailment_penalty': curtail_penalty,
        'common_ac_cost': squared_penalty + curtail_penalty,
        'model_objective_sum': float(arrays['model_objective'][sl].sum()),
        'model_slack_sum_sq': float(arrays['slack_plus'][sl].sum() + arrays['slack_minus'][sl].sum()),
        'prediction_error_rmse_sq': float(np.sqrt(np.mean(error_sq ** 2))),
        'prediction_error_max_abs_sq': float(abs(error_sq).max()),
        'ac_power_flow_count': len(v), 'ac_failures': 0,
    }


def export_csv(out, data, series, metrics):
    # 一套列名与阈值；节点使用物理编号2..33。
    with (out / 'metrics.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['window', 'metric', 'M1', 'M2', 'M2_minus_M1'])
        for window in ('full_day', 'after_shared_initialization'):
            for key, val in metrics['M1'][window].items():
                second = metrics['M2'][window][key]
                writer.writerow([window, key, val, second, second - val])
    with (out / 'period_comparison.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['method', 'period', 'v_min_pu', 'v_max_pu', 'under_nodes', 'over_nodes',
                         'curtailment_kw', 'loss_p_kw', 'grid_p_kw', 'grid_q_kvar',
                         'actual_voltage_penalty', 'actual_curtailment_penalty', 'common_ac_cost',
                         'model_objective', 'model_slack_sum_sq', 'prediction_max_error_sq'])
        for method, a in series.items():
            for t in range(data.periods):
                v, vsq = a['voltage_pu'][t, 1:], a['voltage_sq'][t]
                penalty = data.cv * (np.maximum(vsq - data.vhi, 0) + np.maximum(data.vlo - vsq, 0)).sum()
                cpen = data.ccurt * a['curtailment_kw'][t] / data.power_base_kw
                writer.writerow([method, t, v.min(), v.max(), (v < data.vmin - VOLTAGE_COUNT_TOL_PU).sum(),
                                 (v > data.vmax + VOLTAGE_COUNT_TOL_PU).sum(), a['curtailment_kw'][t],
                                 a['loss_p_kw'][t], a['grid_p_kw'][t], a['grid_q_kvar'][t],
                                 penalty, cpen, penalty + cpen, a['model_objective'][t],
                                 (a['slack_plus'][t] + a['slack_minus'][t]).sum(),
                                 abs(vsq - a['nominal_sq'][t]).max()])
    with (out / 'node_voltage_comparison.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['method', 'period', 'physical_bus', 'actual_voltage_pu', 'actual_voltage_sq',
                         'model_nominal_sq', 'robust_lower_sq', 'robust_upper_sq', 'slack_plus', 'slack_minus'])
        for method, a in series.items():
            for t in range(data.periods):
                for j in range(data.n):
                    writer.writerow([method, t, j + 2, a['voltage_pu'][t, j + 1], a['voltage_sq'][t, j],
                                     a['nominal_sq'][t, j], a['lower_sq'][t, j], a['upper_sq'][t, j],
                                     a['slack_plus'][t, j], a['slack_minus'][t, j]])
    with (out / 'device_comparison.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['method', 'period', 'physical_bus', 'kind', 'value', 'unit'])
        for method, a in series.items():
            for t in range(data.periods):
                for j, (kind, bus) in enumerate(data.controls):
                    writer.writerow([method, t, bus, kind, a['u'][t, j] * data.power_base_kw,
                                     'kW' if kind in ('curt', 'pess') else 'kvar'])
                for j, bus in enumerate(data.ess):
                    for kind in ('pch', 'pdis'):
                        writer.writerow([method, t, bus, kind, a[kind][t, j] * data.power_base_kw, 'kW'])
                    for kind, boundary in (('energy_before', t), ('energy_after', t + 1)):
                        writer.writerow([method, t, bus, kind,
                                         a['energy_boundaries_pu_h'][boundary, j] * data.power_base_kw, 'kWh'])
                    for kind in ('zch', 'zdis'):
                        writer.writerow([method, t, bus, kind, a[kind][t, j], 'binary'])
                for j, bus in enumerate(data.cb):
                    writer.writerow([method, t, bus, 'gamma', a['gamma'][t, j], 'step'])
