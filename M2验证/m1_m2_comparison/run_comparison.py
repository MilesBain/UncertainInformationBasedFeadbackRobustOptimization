"""在严格一致的物理环境和数值设置下运行M1/M2，并统一使用AC结果评价。"""
from __future__ import annotations

import argparse
import copy
import platform
import sys
from pathlib import Path
import numpy as np
import scipy
import gurobipy as gp
from shared import (ROOT, ModelData, load_legacy, ACPlant, solve_step, verify_sources,
                    scenario, compare_scenarios, numeric_guard_audit, digest, write_json,
                    verify_devices, verify_step, verify_power_flow)
from baseline import solve_m1
from evaluation import arrays_from_trace, evaluate, export_csv
from robust_comparison import evaluate_robust, export_robust_csv
from audit_checks import (audit_objective_coefficients, verify_initial_state,
                          export_initial_state, paired_ac_validation)


def max_checks(checks):
    names = sorted(set().union(*(c.keys() for c in checks)))
    return {key: max(c.get(key, 0) for c in checks) for key in names}


def run(out, time_limit=120, solver_log=False, figures=True):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / 'progress.json', {'status': 'running', 'phase': 'checking inputs'})
    source_audit = verify_sources()
    # 独立实例化两份环境，精确检查每个参数/数组，而非仅声明共用配置。
    d1, d2 = ModelData(load_legacy()), ModelData(load_legacy())
    plant1, plant2 = ACPlant(d1.legacy), ACPlant(d2.legacy)
    plant1.check_box_membership(d1)
    plant2.check_box_membership(d2)
    initial_scenario1, initial_scenario2 = scenario(d1, plant1, time_limit), scenario(d2, plant2, time_limit)
    equality = compare_scenarios(initial_scenario1, initial_scenario2)
    audit = dict(equality, source_files=source_audit, numerical_guards=numeric_guard_audit(d1))
    audit['objective_coefficients'] = audit_objective_coefficients(d1, d2)
    write_json(out / 'shared_scenario.json', initial_scenario1)
    write_json(out / 'fairness_audit.json', audit)
    m1_actions, m1_pred, m1_info, m1_checks, m1_raw = solve_m1(d1, solver_log, time_limit)
    print('M1 original full-day model optimal; objective=%.10f' % m1_info['objective'], flush=True)
    # 同一份M1最优解第0时段动作深拷贝给M2，不再次求解初始化以免多解改变初态。
    initial_action = copy.deepcopy(m1_actions[0])
    m1_checks.append(verify_devices(d1, 0, m1_actions[0], d1.initial_energy))
    m2_checks = [verify_devices(d2, 0, initial_action, d2.initial_energy)]
    initial_m1_flow, initial_flow, check1, check2, pair0 = paired_ac_validation(
        d1, d2, plant1, plant2, 0, m1_actions[0], initial_action)
    m1_checks.append(check1)
    m2_checks.append(check2)
    m1_flows = [initial_m1_flow]
    m1_energies = [d1.initial_energy.copy(), m1_actions[0].energy_after.copy()]
    m2_actions, m2_flows = [initial_action], [initial_flow]
    m2_energies = [d2.initial_energy.copy(), initial_action.energy_after.copy()]
    m2_pred = [copy.deepcopy(m1_pred[0])]
    audit['shared_initial_state'] = verify_initial_state(
        d1, d2, m1_actions[0], initial_action, initial_m1_flow, initial_flow)
    export_initial_state(out, d1, d2, m1_actions[0], initial_action, audit['shared_initial_state'])
    paired_records = [pair0]
    step_info, feedback, control_inputs, steps = [], [], [], []
    unchanged_checks = []
    for t in range(d2.periods - 1):
        v, u, energy = m2_flows[-1].voltage_sq.copy(), m2_actions[-1].u.copy(), m2_energies[-1].copy()
        result = solve_step(d2, t, v, u, energy, 'dual', solver_log, time_limit, out / 'diagnostics')
        m2_checks.append(verify_step(d2, t, v, u, energy, result))
        m1_checks.append(verify_devices(d1, t + 1, m1_actions[t + 1], m1_energies[-1]))
        flow1, flow, check1, check2, pair = paired_ac_validation(
            d1, d2, plant1, plant2, t + 1, m1_actions[t + 1], result.dispatch)
        m1_checks.append(check1)
        m2_checks.append(check2)
        paired_records.append(pair)
        new_scenario1, new_scenario2 = scenario(d1, plant1, time_limit), scenario(d2, plant2, time_limit)
        compare_scenarios(initial_scenario1, new_scenario1)
        compare_scenarios(initial_scenario2, new_scenario2)
        unchanged_checks.append({'period': t + 1, 'scenario_unchanged': True,
                                 'm1_sha256': digest(new_scenario1), 'm2_sha256': digest(new_scenario2)})
        feedback.append(v)
        control_inputs.append(u)
        m1_flows.append(flow1)
        m1_energies.append(m1_actions[t + 1].energy_after.copy())
        m2_actions.append(result.dispatch)
        m2_flows.append(flow)
        m2_energies.append(result.dispatch.energy_after.copy())
        m2_pred.append({'nominal_sq': result.v_nominal_sq, 'lower_sq': result.v_lower_sq,
                        'upper_sq': result.v_upper_sq, 'sp': result.slack_plus, 'sm': result.slack_minus,
                        'objective': result.info['objective']})
        step_info.append(result.info)
        steps.append(result)
        write_json(out / 'progress.json', {'status': 'running', 'M1_ac_periods': len(m1_flows),
                                           'M2_ac_periods': len(m2_flows), 'last_completed_period': t + 1})
        print('M2 period %02d optimal; actual voltage [%.6f, %.6f]' %
              (t + 1, flow.voltage_pu[1:].min(), flow.voltage_pu[1:].max()), flush=True)
    m1 = arrays_from_trace(d1, m1_actions, m1_flows, m1_pred, m1_energies)
    m1['original_milp_solution'] = m1_raw
    np.savez_compressed(out / 'm1_results.npz', **m1)
    m2 = arrays_from_trace(d2, m2_actions, m2_flows, m2_pred, m2_energies)
    m2['feedback_voltage_sq'] = np.array(feedback)
    m2['feedback_control'] = np.array(control_inputs)
    for key in ('h', 'lambda_plus', 'lambda_minus'):
        m2[key] = np.array([getattr(s, key) for s in steps])
    np.testing.assert_array_equal(m2['feedback_voltage_sq'], m2['voltage_sq'][:-1])
    np.testing.assert_array_equal(m2['feedback_control'], m2['u'][:-1])
    np.savez_compressed(out / 'm2_results.npz', **m2)
    audit['per_period_unchanged'] = unchanged_checks
    audit['feedback_exactly_ac_measured'] = True
    audit['paired_ac_validation_periods'] = len(paired_records)
    audit['both_actual_voltage_sources'] = 'PYPOWER.runpf (48 calls: M1 24, M2 24)'
    write_json(out / 'paired_ac_audit.json', paired_records)
    audit['final_source_verification'] = verify_sources()
    write_json(out / 'fairness_audit.json', audit)

    series = {'M1': m1, 'M2': m2}
    metrics = {method: {'full_day': evaluate(d1, arr, 0),
                        'after_shared_initialization': evaluate(d1, arr, 1)} for method, arr in series.items()}
    robust_metrics = {method: {'comparison_periods_1_to_23': evaluate_robust(d1, arr, 1),
                               'full_day_including_shared_initialization': evaluate_robust(d1, arr, 0)}
                      for method, arr in series.items()}
    metrics['M1']['solver'] = m1_info
    metrics['M2']['solver'] = {
        'status': 'all_optimal', 'number_of_rolling_solves': len(step_info),
        'rolling_runtime_sum_s': sum(s['runtime_s'] for s in step_info),
        'rolling_runtime_mean_s': float(np.mean([s['runtime_s'] for s in step_info])),
        'rolling_runtime_max_s': max(s['runtime_s'] for s in step_info),
        'rolling_build_and_solve_sum_s': sum(s['build_and_solve_s'] for s in step_info),
        'shared_M1_initialization_runtime_s': m1_info['solver_runtime_s'],
        'runtime_including_initialization_s': m1_info['solver_runtime_s'] + sum(s['runtime_s'] for s in step_info),
        'max_gap': max(s['gap'] for s in step_info), 'optimization_failures': 0}
    summary = {'status': 'passed', 'scenario_sha256': equality['m1_sha256'],
               'parameters_exactly_equal': True, 'shared_initial_state_exactly_equal': True,
               'robust_prediction_metrics': robust_metrics,
               'paired_ac_validation_periods': len(paired_records),
               'metrics': metrics, 'maximum_validation_residuals': {'M1': max_checks(m1_checks), 'M2': max_checks(m2_checks)},
               'methodology': {
                   'M1': 'Original 24-period static robust MILP with intertemporal storage; one open-loop solve.',
                   'M2': 'Same M1 initial action; 23 one-step robust solves with measured AC voltage feedback.',
                   'information_horizon_difference': 'M1 can schedule storage over all 24 periods; M2 uses only t and t+1.',
                   'common_ac_cost': 'C_V * sum(max(V_AC^2 - Vmax^2, 0) + max(Vmin^2 - V_AC^2, 0)) + C_CURT * sum(P_curt_pu)',
                   'objective_warning': 'Model objectives are diagnostic; compare actual AC cost and physical metrics.',
                   'separate_prediction_and_actual': 'Compare M1 vs M2 robust predictions; separately compare AC(M1 controls) vs AC(M2 controls).',
                   'prediction_window': 'Primary robust comparison is periods 1..23; period 0 in M2 is shared M1 initialization, not an M2 forecast.',
                   'terminal_energy': 'No terminal SOC condition or terminal energy value in either original model.',
                   'degeneracy': 'No action smoothing or extra tie-breaking objective; optimal controls can be non-unique.'},
               'environment': {'python': platform.python_version(), 'numpy': np.__version__, 'scipy': scipy.__version__,
                               'gurobi': '.'.join(map(str, gp.gurobi.version())), 'python_executable': sys.executable}}
    write_json(out / 'summary.json', summary)
    write_json(out / 'validation_details.json', {'M1': m1_checks, 'M2': m2_checks, 'M2_solver_steps': step_info})
    export_csv(out, d1, series, metrics)
    export_robust_csv(out, d1, series, robust_metrics)
    if figures:
        from plot_comparison import plot_all
        plot_all(out)
    from report import write_report
    write_report(out, summary, audit)
    write_json(out / 'progress.json', {'status': 'passed', 'M1_ac_periods': 24, 'M2_ac_periods': 24})
    previous_failure = out / 'failure.json'
    if previous_failure.exists():
        previous_failure.unlink()
    print('Comparison PASSED. M1/M2 physical scenario SHA256: ' + equality['m1_sha256'], flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'results')
    parser.add_argument('--time-limit', type=float, default=120)
    parser.add_argument('--solver-log', action='store_true')
    parser.add_argument('--no-figures', action='store_true')
    args = parser.parse_args()
    try:
        run(args.output, args.time_limit, args.solver_log, not args.no_figures)
    except Exception as exc:
        args.output.mkdir(parents=True, exist_ok=True)
        write_json(args.output / 'failure.json', {'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)})
        raise


if __name__ == '__main__':
    main()
