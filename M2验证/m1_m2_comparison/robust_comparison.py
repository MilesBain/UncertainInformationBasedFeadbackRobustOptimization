"""预测鲁棒结果相互比较，以及各自预测包络与各自AC结果的一致性。"""
from __future__ import annotations

import csv
import numpy as np

ROBUST_TOL_SQ = 2e-7


def voltage_value(value):
    return float(np.sqrt(value)) if value >= 0 else None


def evaluate_robust(data, arrays, start=1):
    """默认只比较真正有M2预测的目标时段1..23，AC值不参与预测指标。"""
    lo, hi = arrays['lower_sq'][start:], arrays['upper_sq'][start:]
    nominal = arrays['nominal_sq'][start:]
    sp, sm = arrays['slack_plus'][start:], arrays['slack_minus'][start:]
    actual = arrays['voltage_sq'][start:]
    if not all(np.isfinite(x).all() for x in (lo, hi, nominal, sp, sm, actual)):
        raise AssertionError('Non-finite prediction/AC array.')
    if np.any(lo > hi + ROBUST_TOL_SQ):
        raise AssertionError('Reversed robust prediction interval.')
    lower_viol = np.maximum(data.vlo - lo, 0)
    upper_viol = np.maximum(hi - data.vhi, 0)
    required = lower_viol + upper_viol
    below = np.maximum(lo - actual, 0)
    above = np.maximum(actual - hi, 0)
    predicted_bad = (lower_viol > ROBUST_TOL_SQ) | (upper_viol > ROBUST_TOL_SQ)
    outside = (below > ROBUST_TOL_SQ) | (above > ROBUST_TOL_SQ)
    voltage_penalty = float(data.cv * (sp + sm).sum())
    curtailment_penalty = float(data.ccurt * arrays['curtailment_kw'][start:].sum() / data.power_base_kw)
    objective = float(arrays['model_objective'][start:].sum())
    residual = max(float(np.max(np.maximum(upper_viol - sp, 0))),
                   float(np.max(np.maximum(lower_viol - sm, 0))))
    if residual > ROBUST_TOL_SQ:
        raise AssertionError('Robust prediction violates the relaxed voltage limits.')
    if abs(objective - voltage_penalty - curtailment_penalty) > 2e-7 * (1 + abs(objective)):
        raise AssertionError('Predicted objective components do not match the recorded objective.')
    valid_pu = bool(np.all(lo >= 0) and np.all(hi >= 0))
    width_pu = np.sqrt(hi) - np.sqrt(lo) if valid_pu else None
    return {
        'first_target_period': start, 'target_periods': len(lo), 'node_periods': int(lo.size),
        'robust_lower_min_sq': float(lo.min()), 'robust_upper_max_sq': float(hi.max()),
        'robust_lower_min_pu': voltage_value(float(lo.min())),
        'robust_upper_max_pu': voltage_value(float(hi.max())),
        'nominal_prediction_min_sq': float(nominal.min()), 'nominal_prediction_max_sq': float(nominal.max()),
        'mean_interval_width_sq': float((hi - lo).mean()),
        'max_interval_width_sq': float((hi - lo).max()),
        'mean_interval_width_pu': float(width_pu.mean()) if valid_pu else None,
        'negative_squared_lower_node_periods': int((lo < 0).sum()),
        'robust_hard_limit_violation_node_periods': int(predicted_bad.sum()),
        'robust_hard_limit_violation_periods': int(predicted_bad.any(axis=1).sum()),
        'required_worst_case_slack_sum_sq': float(required.sum()),
        'slack_plus_sum_sq': float(sp.sum()), 'slack_minus_sum_sq': float(sm.sum()),
        'voltage_slack_sum_sq': float((sp + sm).sum()),
        'max_relaxed_robust_constraint_violation_sq': residual,
        'predicted_voltage_penalty': voltage_penalty, 'predicted_curtailment_penalty': curtailment_penalty,
        'predicted_robust_objective_sum': objective,
        'ac_below_prediction_interval_node_periods': int((below > ROBUST_TOL_SQ).sum()),
        'ac_above_prediction_interval_node_periods': int((above > ROBUST_TOL_SQ).sum()),
        'ac_outside_prediction_interval_node_periods': int(outside.sum()),
        'ac_prediction_interval_coverage_pct': float(100 * (1 - outside.mean())),
        'max_ac_prediction_interval_exceedance_sq': float(np.maximum(below, above).max()),
        'nominal_vs_ac_rmse_sq': float(np.sqrt(np.mean((actual - nominal) ** 2))),
        'nominal_vs_ac_max_error_sq': float(np.max(abs(actual - nominal))),
    }


def export_robust_csv(out, data, series, metrics):
    with (out / 'robust_prediction_metrics.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['window', 'metric', 'M1_prediction', 'M2_prediction', 'M2_minus_M1'])
        for window in ('comparison_periods_1_to_23', 'full_day_including_shared_initialization'):
            for key, first in metrics['M1'][window].items():
                second = metrics['M2'][window][key]
                delta = second - first if first is not None and second is not None else ''
                writer.writerow([window, key, first, second, delta])
    with (out / 'robust_prediction_periods.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['method', 'target_period', 'prediction_source', 'robust_lower_min_sq',
                         'robust_upper_max_sq', 'robust_lower_min_pu', 'robust_upper_max_pu',
                         'mean_interval_width_sq', 'slack_plus_sum_sq', 'slack_minus_sum_sq',
                         'predicted_voltage_penalty', 'predicted_curtailment_penalty',
                         'predicted_objective', 'relaxed_robust_violation_sq',
                         'ac_interval_coverage_pct', 'max_ac_interval_exceedance_sq'])
        for method, a in series.items():
            for t in range(data.periods):
                lo, hi = a['lower_sq'][t], a['upper_sq'][t]
                plus, minus = float(a['slack_plus'][t].sum()), float(a['slack_minus'][t].sum())
                actual = a['voltage_sq'][t]
                distance = np.maximum(np.maximum(lo - actual, actual - hi), 0)
                residual = max(0, float(np.max(hi - data.vhi - a['slack_plus'][t])),
                               float(np.max(data.vlo - lo - a['slack_minus'][t])))
                source = 'shared_M1_initialization' if method == 'M2' and t == 0 else method + '_prediction'
                writer.writerow([method, t, source, lo.min(), hi.max(), voltage_value(lo.min()),
                                 voltage_value(hi.max()), (hi - lo).mean(), plus, minus,
                                 data.cv * (plus + minus), data.ccurt * a['curtailment_kw'][t] / data.power_base_kw,
                                 a['model_objective'][t], residual, 100 * (distance <= ROBUST_TOL_SQ).mean(), distance.max()])
    with (out / 'four_way_voltage_comparison.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        fields = ['target_period', 'physical_bus', 'M2_prediction_source']
        for method in ('M1', 'M2'):
            fields += [method + suffix for suffix in ('_predicted_lower_sq', '_predicted_nominal_sq',
                       '_predicted_upper_sq', '_ac_verified_sq', '_predicted_lower_pu',
                       '_predicted_upper_pu', '_ac_verified_pu', '_ac_outside_prediction_interval')]
        writer.writerow(fields)
        for t in range(data.periods):
            for j in range(data.n):
                row = [t, j + 2, 'shared_M1_initialization' if t == 0 else 'M2_prediction']
                for method in ('M1', 'M2'):
                    a = series[method]
                    lo, hi, actual = a['lower_sq'][t, j], a['upper_sq'][t, j], a['voltage_sq'][t, j]
                    row += [lo, a['nominal_sq'][t, j], hi, actual, voltage_value(lo), voltage_value(hi),
                            a['voltage_pu'][t, j + 1], bool(actual < lo - ROBUST_TOL_SQ or actual > hi + ROBUST_TOL_SQ)]
                writer.writerow(row)
