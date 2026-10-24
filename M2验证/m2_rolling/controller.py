"""文稿第6节：真实电压反馈下的单步鲁棒 MILP。"""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
import numpy as np
import gurobipy as gp
from gurobipy import GRB


@dataclass
class Dispatch:
    u: np.ndarray
    pch: np.ndarray
    pdis: np.ndarray
    zch: np.ndarray
    zdis: np.ndarray
    gamma: np.ndarray
    energy_after: np.ndarray


@dataclass
class StepResult:
    dispatch: Dispatch
    slack_plus: np.ndarray
    slack_minus: np.ndarray
    h: np.ndarray
    v_nominal_sq: np.ndarray
    v_lower_sq: np.ndarray
    v_upper_sq: np.ndarray
    lambda_plus: object
    lambda_minus: object
    info: dict


def _configure(model, log, time_limit):
    model.Params.OutputFlag = int(log)
    model.Params.Threads = 1
    model.Params.Seed = 0
    model.Params.MIPGap = 1e-8
    model.Params.MIPGapAbs = 1e-10
    model.Params.FeasibilityTol = 1e-8
    model.Params.IntFeasTol = 1e-8
    if time_limit is not None:
        if time_limit <= 0:
            raise ValueError('Time limit must be positive.')
        model.Params.TimeLimit = time_limit


def initialize_m1(data, log=False, time_limit=120):
    """完整求解未改动的M1约束，只提取第0时段动作及E[1]；其余计划不用。"""
    old = data.legacy
    builder = old.b
    from scipy.sparse import coo_matrix
    rr, cc, vv, lower, upper = [], [], [], [], []
    for row, (coef, lo, hi) in enumerate(builder.rows):
        lower.append(lo)
        upper.append(hi)
        for col, value in coef.items():
            if value:
                rr.append(row)
                cc.append(col)
                vv.append(value)
    mat = coo_matrix((vv, (rr, cc)), shape=(len(lower), len(builder.names))).tocsr()
    lo, hi = np.array(lower), np.array(upper)
    objective = np.zeros(len(builder.names))
    for col, value in builder.obj.items():
        objective[col] = value
    with gp.Model('M1_initial_state') as model:
        _configure(model, log, time_limit)
        lb, ub = np.array(builder.lb), np.array(builder.ub)
        integer = np.array(builder.integrality) == 1
        binary = integer & (lb == 0) & (ub == 1)
        x = model.addMVar(len(lb), lb=lb, ub=ub,
                         vtype=np.where(binary, 'B', np.where(integer, 'I', 'C')))
        model.setObjective(objective @ x)
        equal = np.isfinite(lo) & np.isfinite(hi) & (lo == hi)
        for mask, sense, rhs in [(equal, '=', lo),
                                 (np.isfinite(lo) & ~equal, '>', lo),
                                 (np.isfinite(hi) & ~equal, '<', hi)]:
            if mask.any():
                model.addMConstr(mat[mask], x, sense, rhs[mask])
        model.optimize()
        if model.Status != GRB.OPTIMAL:
            raise RuntimeError('M1 initialization is not optimal; status=%s' % model.Status)
        values = old.extract_results(x.X)
        info = {'status': 'optimal', 'gurobi_status': int(model.Status),
                'objective': float(model.ObjVal), 'runtime_s': float(model.Runtime),
                'gap': float(model.MIPGap), 'source': 'unmodified full-horizon M1, period 0 only'}
    u = np.zeros(data.nu)
    for j, (kind, bus) in enumerate(data.controls):
        if kind == 'qcb':
            u[j] = values['gam'][0, bus - 1] * data.cb_params[bus][0]
        else:
            u[j] = values['qsvc' if kind == 'qsvg' else kind][0, bus - 1]
    ess_values = lambda name: np.array([values[name][0, b - 1] for b in data.ess])
    dispatch = Dispatch(u, ess_values('pch'), ess_values('pdis'), ess_values('zch'),
                        ess_values('zdis'), np.array([values['gam'][0, b - 1] for b in data.cb]),
                        np.array([values['E'][1, b - 1] for b in data.ess]))
    return dispatch, info


def solve_step(data, t, v_measured_sq, u_actual, energy_before, formulation='dual',
               log=False, time_limit=120, failure_dir=None):
    """t->t+1。energy_before是目标时段t+1执行前能量；每步只推进一次。"""
    if formulation not in ('dual', 'absolute'):
        raise ValueError('formulation must be dual or absolute')
    v = np.asarray(v_measured_sq, dtype=float)
    actual = np.asarray(u_actual, dtype=float)
    energy = np.asarray(energy_before, dtype=float)
    if v.shape != (data.n,) or actual.shape != (data.nu,) or energy.shape != (len(data.ess),):
        raise ValueError('Invalid feedback/state dimensions.')
    if not all(np.isfinite(a).all() for a in (v, actual, energy)) or np.any(v <= 0):
        raise ValueError('Feedback and state must be finite; squared voltage must be positive.')
    for j, bus in enumerate(data.ess):
        p = data.ess_params[bus]
        cap = p['e_capacity'] / data.power_base_kw
        if not p['soc_min'] * cap - 1e-8 <= energy[j] <= p['soc_max'] * cap + 1e-8:
            raise ValueError('Input storage energy is outside the physical bounds.')
    h0 = data.h_external(t)
    started = perf_counter()
    with gp.Model('M2_%02d_%s' % (t + 1, formulation)) as model:
        _configure(model, log, time_limit)
        u = model.addMVar(data.nu, lb=-GRB.INFINITY, name='u_next')
        sp = model.addMVar(data.n, lb=0, name='s_plus')
        sm = model.addMVar(data.n, lb=0, name='s_minus')
        for bus in data.pv:
            curt, qpv = u[data.index['curt', bus]], u[data.index['qpv', bus]]
            available = data.p_available[t + 1, bus - 2]
            model.addConstr(curt >= 0)
            model.addConstr(curt <= available)
            model.addConstr(qpv <= data.kappa * (available - curt))
            model.addConstr(qpv >= -data.kappa * (available - curt))
        for bus in data.svg:
            qsvg = u[data.index['qsvg', bus]]
            model.addConstr(qsvg <= data.svg_max[bus])
            model.addConstr(qsvg >= -data.svg_max[bus])
        gamma = model.addMVar(len(data.cb), lb=0, ub=[data.cb_params[b][1] for b in data.cb],
                             vtype=GRB.INTEGER, name='cb_steps')
        for j, bus in enumerate(data.cb):
            model.addConstr(u[data.index['qcb', bus]] == data.cb_params[bus][0] * gamma[j])
        ne = len(data.ess)
        pch = model.addMVar(ne, lb=0, name='p_ch')
        pdis = model.addMVar(ne, lb=0, name='p_dis')
        zch = model.addMVar(ne, vtype=GRB.BINARY, name='z_ch')
        zdis = model.addMVar(ne, vtype=GRB.BINARY, name='z_dis')
        eafter = model.addMVar(ne, lb=0, name='energy_after')
        for j, bus in enumerate(data.ess):
            p = data.ess_params[bus]
            base = data.power_base_kw
            model.addConstr(pch[j] <= p['p_ch_max'] / base * zch[j])
            model.addConstr(pdis[j] <= p['p_dis_max'] / base * zdis[j])
            model.addConstr(zch[j] + zdis[j] <= 1)
            model.addConstr(u[data.index['pess', bus]] == pdis[j] - pch[j])
            model.addConstr(u[data.index['qess', bus]] <= p['q_max'] / base)
            model.addConstr(u[data.index['qess', bus]] >= -p['q_max'] / base)
            model.addConstr(eafter[j] == energy[j] + data.dt *
                            (p['eta_ch'] * pch[j] - pdis[j] / p['eta_dis']))
            model.addConstr(eafter[j] >= p['soc_min'] * p['e_capacity'] / base)
            model.addConstr(eafter[j] <= p['soc_max'] * p['e_capacity'] / base)

        # 保留M1给定的主网交换范围，不增设根节点控制变量或支路容量约束。
        # 此处为忽略网损的交换功率；AC实际交换含损耗，另行记录与检查。
        pgrid = -data.p_ex[t + 1].sum() - data.Bp.sum(axis=0) @ u
        qgrid = -data.q_ex[t + 1].sum() - data.Bq.sum(axis=0) @ u
        model.addConstr(pgrid >= data.pm_lim[0])
        model.addConstr(pgrid <= data.pm_lim[1])
        model.addConstr(qgrid >= data.qm_lim[0])
        model.addConstr(qgrid <= data.qm_lim[1])

        d = 2 * data.n
        lp = lm = None
        if formulation == 'dual':
            # F=[I;-I]；完整保留已知线路退化区间，无裁剪或采样近似。
            lp = model.addMVar((data.n, 2 * d), lb=0, name='lambda_plus')
            lm = model.addMVar((data.n, 2 * d), lb=0, name='lambda_minus')
            for i in range(data.n):
                h = h0[i] + data.K[i] @ (u - actual)
                model.addConstr(lp[i, :d] - lp[i, d:] == h, name='dual_upper_%d' % i)
                model.addConstr(lm[i, :d] - lm[i, d:] == -h, name='dual_lower_%d' % i)
                model.addConstr(data.g @ lp[i] <= data.vhi + sp[i] - v[i])
                model.addConstr(data.g @ lm[i] <= v[i] - data.vlo + sm[i])
        else:
            # 独立数学验算形式：盒集支持函数 theta_nom*h + radius*abs(h)。
            z = model.addMVar((data.n, d), lb=0, name='abs_h')
            for i in range(data.n):
                h = h0[i] + data.K[i] @ (u - actual)
                model.addConstr(z[i] >= h)
                model.addConstr(z[i] >= -h)
                nominal = v[i] + data.theta_nom @ h
                deviation = data.radius @ z[i]
                model.addConstr(nominal + deviation <= data.vhi + sp[i])
                model.addConstr(nominal - deviation >= data.vlo - sm[i])
        curt_indices = [data.index['curt', bus] for bus in data.pv]
        model.setObjective(data.cv * (sp.sum() + sm.sum()) + data.ccurt * u[curt_indices].sum())
        model.optimize()
        if model.Status != GRB.OPTIMAL:
            if failure_dir is not None:
                from pathlib import Path
                out = Path(failure_dir)
                out.mkdir(parents=True, exist_ok=True)
                model.write(str(out / ('failed_%02d.lp' % (t + 1))))
                if model.Status == GRB.INFEASIBLE:
                    model.computeIIS()
                    model.write(str(out / ('failed_%02d.ilp' % (t + 1))))
            raise RuntimeError('M2 period %d not optimal: Gurobi status %d; no state advanced.'
                               % (t + 1, model.Status))
        result_u = u.X.copy()
        h_value = h0 + np.einsum('ijk,k->ij', data.K, result_u - actual)
        nominal_sq = v + h_value @ data.theta_nom
        radius_sq = np.abs(h_value) @ data.radius
        info = {
            'period': t + 1, 'status': 'optimal', 'gurobi_status': int(model.Status),
            'formulation': formulation, 'objective': float(model.ObjVal),
            'voltage_penalty': float(data.cv * (sp.X.sum() + sm.X.sum())),
            'curtailment_penalty': float(data.ccurt * result_u[curt_indices].sum()),
            'runtime_s': float(model.Runtime), 'build_and_solve_s': perf_counter() - started,
            'gap': float(model.MIPGap), 'variables': model.NumVars,
            'constraints': model.NumConstrs, 'integer_variables': model.NumIntVars,
            'uncertainty_sha256': data.uncertainty_digest(),
        }
        dispatch = Dispatch(result_u, pch.X.copy(), pdis.X.copy(), zch.X.copy(),
                            zdis.X.copy(), gamma.X.copy(), eafter.X.copy())
        return StepResult(dispatch, sp.X.copy(), sm.X.copy(), h_value, nominal_sq,
                          nominal_sq - radius_sq, nominal_sq + radius_sq,
                          None if lp is None else lp.X.copy(),
                          None if lm is None else lm.X.copy(), info)
