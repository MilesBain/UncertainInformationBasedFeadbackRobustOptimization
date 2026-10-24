"""对已有轨迹做可复核的能力诊断，不重新优化，不改变环境或控制。"""
from pathlib import Path
import hashlib
import json
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared import ModelData, load_legacy, ACPlant, verify_sources


def main():
    out = Path(__file__).resolve().parent
    results = ROOT / 'results'
    verify_sources()
    data = ModelData(load_legacy())
    plant = ACPlant(data.legacy)
    source_names = ['summary.json', 'fairness_audit.json', 'm1_results.npz', 'm2_results.npz',
                    'paired_ac_audit.json', 'initial_state_audit.json', 'validation_details.json']
    hashes = {name: hashlib.sha256((results / name).read_bytes()).hexdigest() for name in source_names}
    summary = json.loads((results / 'summary.json').read_text(encoding='utf-8'))
    theta_true = np.r_[plant.r_true, plant.x_true]
    assert np.all(theta_true >= data.theta_lo) and np.all(theta_true <= data.theta_hi)
    theta_gap = float(np.max(abs(theta_true - data.theta_nom)))
    info = {'analysis_date': '2026-10-23', 'source_sha256': hashes,
            'scenario_sha256': summary['scenario_sha256'], 'new_optimization_solves': 0,
            'new_ac_solves': 0, 'control_sequences_changed': False,
            'true_parameters_max_distance_from_nominal': theta_gap,
            'true_parameters_inside_box': True, 'methods': {}}
    plotted = {}
    for method in ('M1', 'M2'):
        with np.load(results / (method.lower() + '_results.npz'), allow_pickle=False) as file:
            a = {k: file[k] for k in file.files}
        static, hs = [], []
        for t in range(data.periods):
            p, q = data.injections(t, a['u'][t])
            h = 2 * np.hstack([data.A.T * (data.A @ p), data.A.T * (data.A @ q)])
            hs.append(h)
            static.append(data.legacy.V0_SQ + h @ data.theta_nom)
        static, hs = np.array(static), np.array(hs)
        actual = a['voltage_sq']
        residual = actual - static
        anchored = actual[:-1] + static[1:] - static[:-1]
        anchored_error = actual[1:] - anchored
        identity_error = float(np.max(abs(anchored_error - np.diff(residual, axis=0))))
        prediction_identity = float(np.max(abs((static if method == 'M1' else anchored) -
                                               a['nominal_sq'][0 if method == 'M1' else 1:])))
        assert identity_error < 1e-12 and prediction_identity < 1e-10
        rmse_static = float(np.sqrt(np.mean(residual[1:] ** 2)))
        rmse_anchored = float(np.sqrt(np.mean(anchored_error ** 2)))
        forecast_error = actual[1:] - a['nominal_sq'][1:]
        actions = {}
        for kind in ('qpv', 'qsvg', 'qcb', 'qess', 'pess', 'curt'):
            cols = [j for j, (k, bus) in enumerate(data.controls) if k == kind]
            delta = np.diff(a['u'][:, cols], axis=0) * data.power_base_kw
            actions[kind] = {'total_absolute_change': float(abs(delta).sum()),
                             'max_single_device_change': float(abs(delta).max()),
                             'unit': 'kW' if kind in ('pess', 'curt') else 'kvar'}
        switches = np.diff(a['gamma'], axis=0)
        controlnorm = np.max(abs(np.diff(a['u'], axis=0)), axis=1) * data.power_base_kw
        records = []
        for t, j in np.argwhere((actual > data.vhi + 2.1e-7) | (actual < data.vlo - 1.86e-7)):
            h = hs[t]
            true_linear = data.legacy.V0_SQ + h @ theta_true
            nominal_error = actual[t, j] - a['nominal_sq'][t, j]
            records.append({'period': int(t), 'bus': int(j + 2),
                            'actual_pu': float(a['voltage_pu'][t, j + 1]),
                            'prediction_lower_pu': float(np.sqrt(a['lower_sq'][t, j])),
                            'prediction_upper_pu': float(np.sqrt(a['upper_sq'][t, j])),
                            'prediction_nominal_pu': float(np.sqrt(a['nominal_sq'][t, j])),
                            'prediction_error_sq': float(nominal_error),
                            'absolute_model_residual_before_sq': float(residual[t - 1, j]) if t else None,
                            'absolute_model_residual_after_sq': float(residual[t, j]),
                            'error_over_upper_sq': float(actual[t, j] - a['upper_sq'][t, j]),
                            'max_control_change_any_device': float(controlnorm[t - 1]) if t else None,
                            'true_linear_model_voltage_sq': float(true_linear[j])})
        assert len(records) == summary['metrics'][method]['full_day']['voltage_violation_node_periods']
        cb_movement = float(abs(switches).sum())
        assert abs(cb_movement - round(cb_movement)) < 1e-8
        info['methods'][method] = {
            'same_action_static_prediction_rmse_sq': rmse_static,
            'same_action_ac_anchored_prediction_rmse_sq': rmse_anchored,
            'same_action_rmse_reduction_pct': 100 * (1 - rmse_anchored / rmse_static),
            'same_action_static_prediction_max_abs_error_sq': float(abs(residual[1:]).max()),
            'same_action_anchored_prediction_max_abs_error_sq': float(abs(anchored_error).max()),
            'residual_difference_identity_max_error': identity_error,
            'saved_prediction_identity_max_error': prediction_identity,
            'actual_prediction_p95_abs_error_sq': float(np.quantile(abs(forecast_error), 0.95)),
            'actual_prediction_max_error_location': {
                'period': int(np.unravel_index(abs(forecast_error).argmax(), forecast_error.shape)[0] + 1),
                'bus': int(np.unravel_index(abs(forecast_error).argmax(), forecast_error.shape)[1] + 2)},
            'cb_total_step_movement': int(round(cb_movement)),
            'cb_device_transition_events': int((abs(switches) > 1e-7).sum()),
            'cb_max_single_transition_steps': int(round(abs(switches).max())),
            'control_changes': actions, 'ac_voltage_violations': records,
        }
        plotted[method] = (static, anchored, residual, actual, a)
    (out / 'capability_diagnostics.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.sans-serif': ['Microsoft YaHei', 'SimHei', 'DejaVu Sans'],
                         'axes.unicode_minus': False, 'font.size': 10.5,
                         'axes.spines.top': False, 'axes.spines.right': False})
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.5), constrained_layout=True)
    for ax, method in zip(axs, ('M1', 'M2')):
        static, anchored, residual, actual, a = plotted[method]
        t = np.arange(1, 24)
        ax.plot(t, np.sqrt(np.mean(residual[1:] ** 2, axis=1)), color='#24569b', label='同一动作的静态预测')
        ax.plot(t, np.sqrt(np.mean((actual[1:] - anchored) ** 2, axis=1)), color='#d36d24', label='同一动作的AC锚定预测')
        ax.set_title(method + '控制轨迹保持不变')
        ax.set_xlabel('目标时段')
        ax.set_ylabel('逐时段RMSE / p.u.²')
        ax.set_xticks([1, 4, 8, 12, 16, 20, 23])
        ax.grid(alpha=.18)
        ax.legend(fontsize=8.8)
    for ext in ('png', 'svg'):
        fig.savefig(out / ('feedback_same_action.' + ext), dpi=200)
    plt.close(fig)
    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
