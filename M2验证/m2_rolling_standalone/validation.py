"""求解后从数值重新检查约束，不能只凭Gurobi的optimal就判断模型正确。"""
from itertools import product
import numpy as np
import config
from ac_power_flow import backward_forward_sweep


def largest_violation(values):
    """输入约束的左侧减右侧，返回正的最大违反量（未违反返回0）。"""
    return max(0.0, float(np.max(np.asarray(values))))


def assert_checks(checks, tolerance=config.VALIDATION_TOL):
    for name, error in checks.items():
        if not np.isfinite(error) or error > tolerance:
            raise AssertionError('%s复核失败：%.6g > %.6g' % (name, error, tolerance))
    return checks


def box_vertices(data):
    """只枚举真正变化的8个维度，得到256个64维顶点。"""
    varying = np.flatnonzero(data.theta_radius > 0)
    signs = np.array(list(product([-1.0, 1.0], repeat=len(varying))))
    vertices = np.tile(data.theta_nominal, (len(signs), 1))
    vertices[:, varying] += signs * data.theta_radius[varying]
    return vertices


def validate_dispatch(data, period, action, energy_before):
    """按物理含义检查设备、互斥、SOC、整数档位及无损主网功率约束。"""
    violations = []
    u = action.control
    for values in vars(action).values():
        if not np.isfinite(values).all():
            raise AssertionError('设备动作含非有限值。')
    for bus in data.pv_buses:
        curtailed = u[data.control_index['curt', bus]]
        reactive = u[data.control_index['qpv', bus]]
        available = data.pv_available[period, data.node_index[bus]]
        violations.extend([-curtailed, curtailed - available,
                           abs(reactive) - data.pv_factor * (available - curtailed)])
    for bus in data.svg_buses:
        violations.append(abs(u[data.control_index['qsvg', bus]]) - data.svg_limit[bus])
    for index, bus in enumerate(data.capacitor_buses):
        step_size, max_step = data.capacitors[bus]
        step = action.capacitor_steps[index]
        violations.extend([-step, step - max_step, abs(step - round(step)),
                           abs(u[data.control_index['qcb', bus]] - step_size * step)])
    for index, bus in enumerate(data.storage_buses):
        settings = data.storage[bus]
        capacity = settings['e_capacity'] / data.base_kw
        pch, pdis = action.charge_power[index], action.discharge_power[index]
        zch, zdis = action.charge_on[index], action.discharge_on[index]
        energy = action.energy_after[index]
        expected_energy = energy_before[index] + settings['eta_ch'] * pch * data.dt - pdis * data.dt / settings['eta_dis']
        violations.extend([-pch, -pdis, -zch, zch - 1, -zdis, zdis - 1,
                           abs(zch - round(zch)), abs(zdis - round(zdis)), zch + zdis - 1,
                           pch - settings['p_ch_max'] / data.base_kw * zch,
                           pdis - settings['p_dis_max'] / data.base_kw * zdis,
                           abs(u[data.control_index['pess', bus]] - pdis + pch),
                           abs(u[data.control_index['qess', bus]]) - settings['q_max'] / data.base_kw,
                           settings['soc_min'] * capacity - energy, energy - settings['soc_max'] * capacity,
                           abs(energy - expected_energy)])
    p, q = data.injections(period, u)
    violations.extend([data.grid_p_limits[0] + p.sum(), -p.sum() - data.grid_p_limits[1],
                       data.grid_q_limits[0] + q.sum(), -q.sum() - data.grid_q_limits[1]])
    return assert_checks({'device_constraints': largest_violation(violations)})


def validate_step(data, state, result):
    """同一动作同时通过直接增量、256顶点、对偶证书和目标值复核。"""
    checks = validate_dispatch(data, state.period + 1, result.dispatch, state.energy)
    p0, q0 = data.injections(state.period, state.actual_control)
    p1, q1 = data.injections(state.period + 1, result.dispatch.control)
    direct_h = data.coefficient_matrix(p1 - p0, q1 - q0)
    checks['increment_coefficient'] = float(np.max(np.abs(direct_h - result.coefficient)))
    voltages = state.measured_voltage_sq[:, None] + direct_h @ box_vertices(data).T
    checks['vertex_upper_bound'] = float(np.max(np.abs(voltages.max(axis=1) - result.predicted_upper_sq)))
    checks['vertex_lower_bound'] = float(np.max(np.abs(voltages.min(axis=1) - result.predicted_lower_sq)))
    checks['robust_voltage'] = largest_violation(np.concatenate([
        voltages.max(axis=1) - data.voltage_max_sq - result.slack_upper,
        data.voltage_min_sq - result.slack_lower - voltages.min(axis=1),
        -result.slack_upper, -result.slack_lower]))
    if result.dual_upper is not None:
        checks['dual_equality'] = max(float(np.max(np.abs(result.dual_upper @ data.F - direct_h))),
                                      float(np.max(np.abs(result.dual_lower @ data.F + direct_h))))
        checks['dual_inequality'] = largest_violation(np.concatenate([
            result.dual_upper @ data.g - data.voltage_max_sq - result.slack_upper + state.measured_voltage_sq,
            result.dual_lower @ data.g - state.measured_voltage_sq + data.voltage_min_sq - result.slack_lower,
            -result.dual_upper.ravel(), -result.dual_lower.ravel()]))
    objective = data.voltage_weight * (result.slack_upper.sum() + result.slack_lower.sum())
    objective += data.curtailment_weight * sum(result.dispatch.control[data.control_index['curt', b]] for b in data.pv_buses)
    checks['objective_reconstruction'] = abs(float(objective) - result.info['objective'])
    return assert_checks(checks)


def validate_initial_schedule(data, schedule):
    """核验完整初始化计划的储能时序、256顶点鲁棒约束和总目标值。"""
    energy_before = data.initial_energy
    checks = {'initial_devices': 0.0, 'initial_robust_voltage': 0.0, 'initial_vertex_bounds': 0.0}
    objective = 0.0
    vertices = box_vertices(data)
    for period, action in enumerate(schedule.actions):
        device_error = validate_dispatch(data, period, action, energy_before)['device_constraints']
        checks['initial_devices'] = max(checks['initial_devices'], device_error)
        p, q = data.injections(period, action.control)
        voltages = data.root_voltage ** 2 + data.coefficient_matrix(p, q) @ vertices.T
        error = largest_violation(np.concatenate([
            voltages.max(axis=1) - data.voltage_max_sq - schedule.slack_upper[period],
            data.voltage_min_sq - schedule.slack_lower[period] - voltages.min(axis=1)]))
        checks['initial_robust_voltage'] = max(checks['initial_robust_voltage'], error)
        bounds_error = max(np.max(np.abs(voltages.min(axis=1) - schedule.predicted_lower_sq[period])),
                           np.max(np.abs(voltages.max(axis=1) - schedule.predicted_upper_sq[period])))
        checks['initial_vertex_bounds'] = max(checks['initial_vertex_bounds'], float(bounds_error))
        objective += data.curtailment_weight * sum(action.control[data.control_index['curt', b]] for b in data.pv_buses)
        energy_before = action.energy_after
    objective += data.voltage_weight * (schedule.slack_upper.sum() + schedule.slack_lower.sum())
    checks['initial_objective'] = abs(float(objective) - schedule.info['objective'])
    return assert_checks(checks)


def validate_ac(data, result):
    """用另一套潮流算法、全网功率平衡核验AC结果；电压越限另行统计。"""
    sweep_voltage = backward_forward_sweep(data, result.p_injected, result.q_injected)
    return assert_checks({
        'ac_device_mapping': result.mapping_error,
        'ac_nodal_balance': result.balance_error,
        'ac_sweep_voltage': float(np.max(np.abs(sweep_voltage - result.voltage_complex))),
        'ac_global_p_balance': abs(result.grid_p + result.p_injected.sum() - result.loss_p),
        'ac_global_q_balance': abs(result.grid_q + result.q_injected.sum() - result.loss_q),
        'ac_root_voltage': abs(result.voltage_pu[data.network.root_bus - 1] - data.root_voltage),
    })
