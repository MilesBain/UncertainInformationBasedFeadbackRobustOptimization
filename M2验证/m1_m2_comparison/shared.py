"""加载冻结模型，逐项审计物理场景、模型数值参数及求解设置。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parent
FROZEN = ROOT / 'frozen_model'
sys.path.insert(0, str(FROZEN))
from data import ModelData, load_legacy
from controller import Dispatch, solve_step, _configure
from power_flow import ACPlant
from validation import verify_devices, verify_step, verify_power_flow, box_vertices

SOLVER_SETTINGS = {'Threads': 1, 'Seed': 0, 'MIPGap': 1e-8, 'MIPGapAbs': 1e-10,
                   'FeasibilityTol': 1e-8, 'IntFeasTol': 1e-8}
AC_SETTINGS = {'solver': 'PYPOWER.runpf', 'PF_ALG': 1, 'PF_TOL': 1e-10,
               'PF_MAX_IT': 50, 'root_voltage_pu': 1.0, 'loads_devices': 'constant PQ',
               'capacitors': 'constant Q = gamma * step', 'line_shunt_b_pu': 0.0,
               'branch_capacity_constraint': False, 'AC_root_Q_limit_enforced': False}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def jsonable(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(x) for x in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def canonical(value):
    return json.dumps(jsonable(value), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def verify_sources():
    expected = json.loads((ROOT / 'source_hashes.json').read_text(encoding='utf-8'))
    status = []
    for name, sha in expected.items():
        copy = FROZEN / name
        actual = hashlib.sha256(copy.read_bytes()).hexdigest()
        if actual != sha:
            raise AssertionError('Frozen source modified: ' + name)
        sibling = ROOT.parent / 'm2_rolling' / name
        if sibling.exists() and sibling.read_bytes() != copy.read_bytes():
            raise AssertionError('Live M2 source differs from comparison snapshot: ' + name)
        status.append({'file': name, 'sha256': sha, 'frozen_verified': True,
                       'live_source_present': sibling.exists(),
                       'live_source_matches': True if sibling.exists() else None})
    original = ROOT.parent / 'code' / 'rb_ieee33_storage.py'
    if original.exists() and original.read_bytes() != (FROZEN / 'reference' / original.name).read_bytes():
        raise AssertionError('Original M1 source differs from frozen reference.')
    return status


def scenario(data, plant, time_limit):
    old = data.legacy
    names = ('S_BASE', 'V_BASE', 'Z_BASE', 'T', 'DT', 'BRANCHES', 'LOADS', 'LOAD_SHAPE',
             'PV_SHAPE', 'PV_BUSES', 'PF_MIN', 'KAPPA', 'SVC_BUSES', 'CB_BUSES',
             'ESS_BUSES', 'UNKNOWN', 'PM_LIM', 'QM_LIM', 'V_MIN', 'V_MAX',
             'V_LO_SQ', 'V_HI_SQ', 'V0_SQ', 'C_V', 'C_CURT')
    array_names = ('p_load', 'q_load', 'p_available', 'p_ex', 'q_ex', 'A', 'Bp', 'Bq',
                   'K', 'theta_nom', 'theta_lo', 'theta_hi', 'radius', 'F', 'g', 'initial_energy')
    controller_names = ('n', 'nu', 'periods', 'power_base_kw', 'dt', 'vmin', 'vmax', 'vlo',
                        'vhi', 'cv', 'ccurt', 'kappa', 'pm_lim', 'qm_lim', 'pv', 'ess',
                        'svg', 'cb', 'ess_params', 'svg_max', 'cb_params')
    plant_array_names = ('p_load', 'q_load', 'p_av', 'br_from', 'br_to', 'r_nom', 'x_nom', 'PATH')
    return jsonable({'original_parameters': {name: getattr(old, name) for name in names},
                     'actual_model_arrays': {name: getattr(data, name) for name in array_names},
                     'actual_controller_parameters': {name: getattr(data, name) for name in controller_names},
                     'actual_plant_input_arrays': {name: getattr(old, name) for name in plant_array_names},
                     'control_order': data.controls, 'true_r_pu': plant.r_true,
                     'true_x_pu': plant.x_true, 'ac_settings': AC_SETTINGS,
                     'solver_settings': dict(SOLVER_SETTINGS, TimeLimit=time_limit),
                     'terminal_soc_requirement': None, 'action_cost': 0.0,
                     'forecast_error': 0.0, 'parameter_update': False})


def compare_scenarios(first, second):
    differences = []
    def visit(a, b, path):
        if type(a) is not type(b):
            differences.append(path)
        elif isinstance(a, dict):
            if set(a) != set(b):
                differences.append(path + '/keys')
            for key in set(a) & set(b):
                visit(a[key], b[key], path + '/' + key)
        elif isinstance(a, list):
            if len(a) != len(b):
                differences.append(path + '/length')
            for i, (x, y) in enumerate(zip(a, b)):
                visit(x, y, path + '/' + str(i))
        elif a != b:
            differences.append(path)
    visit(first, second, '')
    if differences:
        raise AssertionError('M1/M2 scenario mismatch: ' + ', '.join(differences[:12]))
    return {'exact_equality': True, 'm1_sha256': digest(first), 'm2_sha256': digest(second),
            'difference_count': 0}


def numeric_guard_audit(data):
    """证明M1宽松的数值截断不构成此场景额外物理限制（外包络）。"""
    old = data.legacy
    p_lo = -data.p_load.copy()
    p_hi = data.p_available - data.p_load
    q_lo = -data.q_load - data.kappa * data.p_available
    q_hi = -data.q_load + data.kappa * data.p_available
    for bus in data.svg:
        q_lo[:, bus - 2] -= data.svg_max[bus]
        q_hi[:, bus - 2] += data.svg_max[bus]
    for bus in data.cb:
        step, count = data.cb_params[bus]
        q_hi[:, bus - 2] += step * count
    for bus in data.ess:
        cfg = data.ess_params[bus]
        p_lo[:, bus - 2] -= cfg['p_ch_max'] / data.power_base_kw
        p_hi[:, bus - 2] += cfg['p_dis_max'] / data.power_base_kw
        q_lo[:, bus - 2] -= cfg['q_max'] / data.power_base_kw
        q_hi[:, bus - 2] += cfg['q_max'] / data.power_base_kw
    abs_p = np.maximum(abs(p_lo @ data.A.T), abs(p_hi @ data.A.T))
    abs_q = np.maximum(abs(q_lo @ data.A.T), abs(q_hi @ data.A.T))
    R = 2 * data.A.T @ np.diag(old.r_nom) @ data.A
    X = 2 * data.A.T @ np.diag(old.x_nom) @ data.A
    v_lo = 1 + p_lo @ R.T + q_lo @ X.T
    v_hi = 1 + p_hi @ R.T + q_hi @ X.T
    psi_bound = (abs_p * old.rho_r + abs_q * old.rho_x) @ data.A
    required_slack = max(0, float(np.max(v_hi + 2 * psi_bound - data.vhi)),
                         float(np.max(data.vlo - v_lo + 2 * psi_bound)))
    values = {'branch_abs_p_upper_bound_pu': float(abs_p.max()),
              'branch_abs_q_upper_bound_pu': float(abs_q.max()),
              'static_nominal_voltage_min_bound_sq': float(v_lo.min()),
              'static_nominal_voltage_max_bound_sq': float(v_hi.max()),
              'psi_upper_bound_sq': float(psi_bound.max()),
              'needed_static_slack_upper_bound_sq': required_slack,
              'pv_abs_q_upper_bound_pu': float((data.kappa * data.p_available).max())}
    safe = (abs_p.max() < 5 and abs_q.max() < 5 and v_lo.min() > -2 and v_hi.max() < 3
            and psi_bound.max() < 2 and required_slack < 1 and values['pv_abs_q_upper_bound_pu'] < 1)
    if not safe:
        raise AssertionError('Legacy artificial bounds cannot be certified redundant: %r' % values)
    values['legacy_numeric_guards_redundant_for_static_model'] = True
    values['explanation'] = ('M1 P/Q and abs-flow +/-5, v0 [-2,3], Psi<=2, slack<=1, '
                             '|Qpv|<=1 do not exclude feasible physical controls in this scenario. '
                             'M2 incremental soft slack remains unbounded as specified.')
    return values
