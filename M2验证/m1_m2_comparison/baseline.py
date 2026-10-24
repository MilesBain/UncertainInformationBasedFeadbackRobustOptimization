"""逐字节冻结的原M1模型：仅适配求解接口与全时序结果导出。"""
from __future__ import annotations

from time import perf_counter
import numpy as np
from scipy.sparse import coo_matrix
import gurobipy as gp
from gurobipy import GRB
from shared import Dispatch, _configure, box_vertices


def solve_m1(data, log=False, time_limit=120):
    old, b = data.legacy, data.legacy.b
    started = perf_counter()
    rr, cc, vv, lo, hi = [], [], [], [], []
    for row, (coefs, lower, upper) in enumerate(b.rows):
        lo.append(lower)
        hi.append(upper)
        for col, val in coefs.items():
            if val:
                rr.append(row)
                cc.append(col)
                vv.append(val)
    matrix = coo_matrix((vv, (rr, cc)), shape=(len(lo), len(b.names))).tocsr()
    lo, hi, lb, ub = map(np.asarray, (lo, hi, b.lb, b.ub))
    objective = np.zeros(len(lb))
    for col, value in b.obj.items():
        objective[col] = value
    with gp.Model('M1_static_full_day') as model:
        _configure(model, log, time_limit)
        integer = np.array(b.integrality) == 1
        binary = integer & (lb == 0) & (ub == 1)
        x = model.addMVar(len(lb), lb=lb, ub=ub,
                         vtype=np.where(binary, 'B', np.where(integer, 'I', 'C')))
        model.setObjective(objective @ x)
        equal = np.isfinite(lo) & np.isfinite(hi) & (lo == hi)
        for mask, sense, rhs in ((equal, '=', lo), (np.isfinite(lo) & ~equal, '>', lo),
                                 (np.isfinite(hi) & ~equal, '<', hi)):
            if mask.any():
                model.addMConstr(matrix[mask], x, sense, rhs[mask])
        model.optimize()
        if model.Status != GRB.OPTIMAL:
            raise RuntimeError('M1 not optimal: Gurobi status=%s' % model.Status)
        solution = x.X.copy()
        info = {'status': 'optimal', 'objective': float(model.ObjVal), 'gap': float(model.MIPGap),
                'solver_runtime_s': float(model.Runtime), 'variables': model.NumVars,
                'constraints': model.NumConstrs, 'integer_variables': model.NumIntVars,
                'assembly_and_solve_s': perf_counter() - started,
                'horizon_periods': data.periods, 'number_of_solves': 1}
    # 原始MILP全部行、变量界和整数性独立残差（不是重建约束的自我验证）。
    lhs = matrix @ solution
    max0 = lambda arr: float(max(0, np.max(arr))) if np.size(arr) else 0.0
    checks = {'m1_original_rows_violation': max(max0(lo - lhs), max0(lhs - hi)),
              'm1_original_bounds_violation': max(max0(lb - solution), max0(solution - ub)),
              'm1_integrality_violation': max0(abs(solution[integer] - np.rint(solution[integer]))),
              'm1_objective_residual': abs(float(objective @ solution) - info['objective'])}
    if max(checks.values()) > 2e-7:
        raise AssertionError('M1 original model constraints failed: %r' % checks)
    values = old.extract_results(solution)
    dispatches, predictions, per_period_checks = [], [], []
    corners = box_vertices(data)
    for t in range(data.periods):
        u = np.zeros(data.nu)
        for j, (kind, bus) in enumerate(data.controls):
            if kind == 'qcb':
                u[j] = values['gam'][t, bus - 1] * data.cb_params[bus][0]
            else:
                u[j] = values['qsvc' if kind == 'qsvg' else kind][t, bus - 1]
        evalues = lambda key: np.array([values[key][t, b - 1] for b in data.ess])
        dispatches.append(Dispatch(u, evalues('pch'), evalues('pdis'), evalues('zch'),
                                   evalues('zdis'), np.array([values['gam'][t, b - 1] for b in data.cb]),
                                   np.array([values['E'][t + 1, b - 1] for b in data.ess])))
        p, q = data.injections(t, u)
        h = 2 * np.hstack([data.A.T * (data.A @ p), data.A.T * (data.A @ q)])
        nominal = 1 + h @ data.theta_nom
        radius = np.abs(h) @ data.radius
        lower, upper = nominal - radius, nominal + radius
        sp, sm = values['sp'][t, 1:], values['sm'][t, 1:]
        extrema = 1 + h @ corners.T
        check = {'m1_static_robust_violation': max(max0(upper - data.vhi - sp), max0(data.vlo - sm - lower)),
                 'm1_static_vertex_residual': max(max0(abs(extrema.max(axis=1) - upper)),
                                                   max0(abs(extrema.min(axis=1) - lower))),
                 'm1_static_nominal_voltage_residual': max0(abs(nominal - values['v0'][t, 1:]))}
        if max(check.values()) > 2e-7:
            raise AssertionError('M1 independent static verification failed: %r' % check)
        per_period_checks.append(check)
        predictions.append({'nominal_sq': nominal, 'lower_sq': lower, 'upper_sq': upper,
                            'sp': sp.copy(), 'sm': sm.copy(),
                            'objective': float(data.cv * (sp.sum() + sm.sum()) +
                                               data.ccurt * sum(u[data.index['curt', b]] for b in data.pv))})
    return dispatches, predictions, info, [checks] + per_period_checks, solution
