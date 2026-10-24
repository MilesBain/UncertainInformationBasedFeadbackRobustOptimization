"""独立数值验收：设备、精确盒集极值、对偶残差、交流物理一致性。"""
from __future__ import annotations

from itertools import product
import numpy as np
from power_flow import backward_forward_check

TOLERANCE = 2e-7


def positive_max(value):
    return float(max(0.0, np.max(np.asarray(value)))) if np.size(value) else 0.0


def verify_devices(data, period, dispatch, energy_before):
    u = dispatch.u
    errors = []
    for bus in data.pv:
        curt, qpv = u[data.index['curt', bus]], u[data.index['qpv', bus]]
        available = data.p_available[period, bus - 2]
        errors += [-curt, curt - available, abs(qpv) - data.kappa * (available - curt)]
    for bus in data.svg:
        errors.append(abs(u[data.index['qsvg', bus]]) - data.svg_max[bus])
    for j, bus in enumerate(data.cb):
        qstep, maxstep = data.cb_params[bus]
        gamma = dispatch.gamma[j]
        errors += [-gamma, gamma - maxstep, abs(gamma - round(gamma)),
                   abs(u[data.index['qcb', bus]] - qstep * gamma)]
    energy_errors = []
    for j, bus in enumerate(data.ess):
        p = data.ess_params[bus]
        base = data.power_base_kw
        ch, dis = dispatch.pch[j], dispatch.pdis[j]
        zch, zdis = dispatch.zch[j], dispatch.zdis[j]
        expected = energy_before[j] + data.dt * (p['eta_ch'] * ch - dis / p['eta_dis'])
        energy_errors.append(abs(dispatch.energy_after[j] - expected))
        errors += [-ch, -dis, ch - zch * p['p_ch_max'] / base,
                   dis - zdis * p['p_dis_max'] / base, zch + zdis - 1,
                   -zch, zch - 1, -zdis, zdis - 1, abs(zch - round(zch)), abs(zdis - round(zdis)),
                   abs(u[data.index['pess', bus]] - dis + ch),
                   abs(u[data.index['qess', bus]]) - p['q_max'] / base,
                   p['soc_min'] * p['e_capacity'] / base - dispatch.energy_after[j],
                   dispatch.energy_after[j] - p['soc_max'] * p['e_capacity'] / base]
    p, q = data.injections(period, u)
    errors += [-p.sum() - data.pm_lim[1], data.pm_lim[0] + p.sum(),
               -q.sum() - data.qm_lim[1], data.qm_lim[0] + q.sum()]
    report = {'device_constraint_violation': positive_max(errors),
              'energy_balance_residual': positive_max(energy_errors)}
    if max(report.values()) > TOLERANCE:
        raise AssertionError('Device/energy feasibility failed: %r' % report)
    return report


def box_vertices(data):
    varying = np.flatnonzero(data.radius > 0)
    if len(varying) > 16:
        raise ValueError('Exhaustive validation is only intended for a small uncertainty box.')
    corners = np.tile(data.theta_nom, (2 ** len(varying), 1))
    corners[:, varying] += np.array(list(product([-1.0, 1.0], repeat=len(varying)))) * data.radius[varying]
    return corners


def verify_step(data, t, voltage_sq, actual, energy_before, result):
    report = verify_devices(data, t + 1, result.dispatch, energy_before)
    # 直接由节点注入差构造H，与模型h0+K*Delta_u两条实现路径核对。
    p0, q0 = data.injections(t, actual)
    p1, q1 = data.injections(t + 1, result.dispatch.u)
    ap, aq = data.A @ (p1 - p0), data.A @ (q1 - q0)
    h = 2 * np.hstack([data.A.T * ap, data.A.T * aq])
    report['h_affine_residual'] = float(np.max(np.abs(h - result.h)))
    # 正负系数逐分量选端点，独立于对偶变量和abs_h辅助变量。
    vmax = voltage_sq + np.maximum(h, 0) @ data.theta_hi + np.minimum(h, 0) @ data.theta_lo
    vmin = voltage_sq + np.maximum(h, 0) @ data.theta_lo + np.minimum(h, 0) @ data.theta_hi
    report['robust_upper_violation'] = positive_max(vmax - data.vhi - result.slack_plus)
    report['robust_lower_violation'] = positive_max(data.vlo - result.slack_minus - vmin)
    report['nonnegative_slack_violation'] = max(positive_max(-result.slack_plus), positive_max(-result.slack_minus))
    report['reported_interval_residual'] = float(max(np.max(abs(vmax - result.v_upper_sq)),
                                                    np.max(abs(vmin - result.v_lower_sq))))
    # 当前4条不确定线路的r/x独立变化，共2^8=256个顶点，逐一穷举。
    corner_v = voltage_sq[:, None] + h @ box_vertices(data).T
    report['vertex_extrema_residual'] = float(max(np.max(abs(corner_v.max(axis=1) - vmax)),
                                                np.max(abs(corner_v.min(axis=1) - vmin))))
    if result.lambda_plus is not None:
        lp, lm = result.lambda_plus, result.lambda_minus
        report['dual_equality_residual'] = float(max(np.max(abs(lp @ data.F - h)),
                                                     np.max(abs(lm @ data.F + h))))
        report['dual_inequality_violation'] = max(
            positive_max(lp @ data.g - data.vhi - result.slack_plus + voltage_sq),
            positive_max(lm @ data.g - voltage_sq + data.vlo - result.slack_minus))
        report['dual_nonnegative_violation'] = max(positive_max(-lp), positive_max(-lm))
        report['weak_duality_violation'] = max(positive_max(vmax - voltage_sq - lp @ data.g),
                                               positive_max(voltage_sq - vmin - lm @ data.g))
    calculated = data.cv * (result.slack_plus.sum() + result.slack_minus.sum())
    calculated += data.ccurt * sum(result.dispatch.u[data.index['curt', b]] for b in data.pv)
    report['objective_residual'] = abs(float(calculated) - result.info['objective'])
    if max(report.values()) > TOLERANCE:
        raise AssertionError('Robust MILP independent validation failed: %r' % report)
    return report


def verify_power_flow(data, plant, pf):
    voltage, iterations = backward_forward_check(data.legacy, pf.p_injected, pf.q_injected,
                                                plant.r_true, plant.x_true)
    difference = float(np.max(np.abs(voltage - pf.v_complex)))
    p_balance = abs(pf.grid_p_pu + pf.p_injected.sum() - pf.loss_p_pu)
    q_balance = abs(pf.grid_q_pu + pf.q_injected.sum() - pf.loss_q_pu)
    report = {'independent_ac_voltage_residual': difference,
              'ac_nodal_balance_residual': pf.balance_residual_pu,
              'device_mapping_residual': pf.mapping_residual_pu,
              'ac_global_power_balance_residual': float(max(p_balance, q_balance))}
    if max(report.values()) > TOLERANCE:
        raise AssertionError('AC independent validation failed: %r' % report)
    return report


def voltage_violations(data, voltage_pu):
    low = np.maximum(data.vmin - voltage_pu, 0)
    high = np.maximum(voltage_pu - data.vmax, 0)
    return {'v_min_pu': float(np.min(voltage_pu)), 'v_max_pu': float(np.max(voltage_pu)),
            'undervoltage_nodes': int(np.sum(low > 1e-7)),
            'overvoltage_nodes': int(np.sum(high > 1e-7)),
            'max_undervoltage_pu': float(np.max(low)), 'max_overvoltage_pu': float(np.max(high))}
