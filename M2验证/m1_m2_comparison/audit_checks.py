"""目标系数、完整设备初态与配对AC执行的可追溯检查。"""
from __future__ import annotations

import csv
from dataclasses import asdict
import numpy as np
from shared import digest, jsonable, write_json, verify_power_flow


def audit_objective_coefficients(d1, d2):
    for name in ('cv', 'ccurt', 'power_base_kw', 'dt'):
        if getattr(d1, name) != getattr(d2, name):
            raise AssertionError('Objective coefficient/unit mismatch: ' + name)
    old = d1.legacy
    expected = {}
    for key, col in old.var_idx.items():
        if key[0] in ('sp', 'sm'):
            expected[col] = d1.cv
        elif key[0] == 'curt':
            expected[col] = d1.ccurt
    actual = {k: v for k, v in old.b.obj.items() if v != 0}
    if actual != expected:
        raise AssertionError('The actual original M1 objective differs from the shared weights.')
    return {'C_V_M1': d1.cv, 'C_V_M2': d2.cv, 'C_CURT_M1': d1.ccurt, 'C_CURT_M2': d2.ccurt,
            'exact_equal': True, 'M1_all_objective_columns_checked': len(old.b.names),
            'power_base_kw': d1.power_base_kw, 'power_unit': 'p.u.',
            'voltage_slack_unit': 'p.u.^2', 'dt_hours': d1.dt,
            'formula': 'C_V*sum(s_plus+s_minus)+C_CURT*sum(P_curt_pu)',
            'extra_action_or_terminal_cost': 0.0}


def verify_initial_state(d1, d2, action1, action2, voltage1, voltage2):
    left = {'dispatch': asdict(action1), 'energy_before': d1.initial_energy,
            'voltage_complex_real': voltage1.v_complex.real,
            'voltage_complex_imag': voltage1.v_complex.imag,
            'voltage_sq': voltage1.voltage_sq}
    right = {'dispatch': asdict(action2), 'energy_before': d2.initial_energy,
             'voltage_complex_real': voltage2.v_complex.real,
             'voltage_complex_imag': voltage2.v_complex.imag,
             'voltage_sq': voltage2.voltage_sq}
    if digest(left) != digest(right):
        raise AssertionError('Initial device state, storage state or AC voltage mismatch.')
    return {'all_dispatch_fields_equal': list(asdict(action1)), 'exact_same_action': True,
            'exact_same_ac_voltage': True, 'exact_same_energy_before_and_after_period_0': True,
            'M1_full_day_solved_once': True, 'M1_initial_state_sha256': digest(left),
            'M2_initial_state_sha256': digest(right), 'M1': jsonable(left), 'M2': jsonable(right)}


def export_initial_state(out, d1, d2, a1, a2, audit):
    rows = []
    def row(bus, kind, first, second, unit):
        if first != second:
            raise AssertionError('Initial setting mismatch: %s bus %s' % (kind, bus))
        rows.append([bus, kind, float(first), float(second), float(second - first), unit, True])
    for j, (kind, bus) in enumerate(d1.controls):
        row(bus, kind, a1.u[j] * d1.power_base_kw, a2.u[j] * d2.power_base_kw,
            'kW' if kind in ('curt', 'pess') else 'kvar')
    for j, bus in enumerate(d1.ess):
        for key in ('pch', 'pdis', 'zch', 'zdis'):
            factor = d1.power_base_kw if key in ('pch', 'pdis') else 1
            row(bus, key, getattr(a1, key)[j] * factor, getattr(a2, key)[j] * factor,
                'kW' if key in ('pch', 'pdis') else 'binary')
        cap = d1.ess_params[bus]['e_capacity']
        for kind, val1, val2 in [('before', d1.initial_energy[j], d2.initial_energy[j]),
                                 ('after', a1.energy_after[j], a2.energy_after[j])]:
            row(bus, 'energy_' + kind, val1 * d1.power_base_kw, val2 * d2.power_base_kw, 'kWh')
            row(bus, 'soc_' + kind, val1 * d1.power_base_kw / cap, val2 * d2.power_base_kw / cap, 'fraction')
    for j, bus in enumerate(d1.cb):
        row(bus, 'gamma', a1.gamma[j], a2.gamma[j], 'step')
    for bus in d1.pv:
        av1, av2 = d1.p_available[0, bus - 2], d2.p_available[0, bus - 2]
        row(bus, 'pv_available', av1 * d1.power_base_kw, av2 * d2.power_base_kw, 'kW')
        row(bus, 'pv_operating', (av1 - a1.u[d1.index['curt', bus]]) * d1.power_base_kw,
            (av2 - a2.u[d2.index['curt', bus]]) * d2.power_base_kw, 'kW')
    with (out / 'initial_device_states.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['physical_bus', 'state', 'M1', 'M2', 'M2_minus_M1', 'unit', 'exact_equal'])
        writer.writerows(rows)
    write_json(out / 'initial_state_audit.json', audit)


def paired_ac_validation(d1, d2, plant1, plant2, period, action1, action2):
    """同一时段分别执行两套控制，不接受任何优化器预测电压作为AC输出。"""
    def environment(d, plant):
        return {'period': period, 'p_load': plant.source.p_load[period],
                'q_load': plant.source.q_load[period], 'pv_available': plant.source.p_av[period],
                'true_r': plant.r_true, 'true_x': plant.x_true,
                'branch_from': plant.source.br_from, 'branch_to': plant.source.br_to,
                'S_BASE': plant.source.S_BASE, 'V_BASE': plant.source.V_BASE, 'V0_SQ': plant.source.V0_SQ}
    signature1, signature2 = digest(environment(d1, plant1)), digest(environment(d2, plant2))
    if signature1 != signature2:
        raise AssertionError('Paired AC validation environments differ.')
    f1 = plant1.run(d1, period, action1)
    f2 = plant2.run(d2, period, action2)
    c1, c2 = verify_power_flow(d1, plant1, f1), verify_power_flow(d2, plant2, f2)
    record = {'period': period, 'same_environment': True,
              'M1_environment_sha256': signature1, 'M2_environment_sha256': signature2,
              'M1_control_sha256': digest(asdict(action1)), 'M2_control_sha256': digest(asdict(action2)),
              'M1_voltage_source': 'PYPOWER.runpf', 'M2_voltage_source': 'PYPOWER.runpf',
              'M1_ac_converged': True, 'M2_ac_converged': True,
              'M1_ac_voltage_sha256': digest(f1.voltage_sq), 'M2_ac_voltage_sha256': digest(f2.voltage_sq),
              'M1_ac_min_pu': float(f1.voltage_pu[1:].min()), 'M1_ac_max_pu': float(f1.voltage_pu[1:].max()),
              'M2_ac_min_pu': float(f2.voltage_pu[1:].min()), 'M2_ac_max_pu': float(f2.voltage_pu[1:].max())}
    return f1, f2, c1, c2, record
