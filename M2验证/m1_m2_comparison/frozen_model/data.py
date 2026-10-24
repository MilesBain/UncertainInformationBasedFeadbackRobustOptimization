"""M1 数据适配、非根节点路径矩阵与固定盒集（功率均为 p.u.）。"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'reference' / 'rb_ieee33_storage.py'
SOURCE_SHA256 = '35b5bd88dce15d12f86d04298175bb7b862c9acb97af4c31552a317c5193ea69'


def load_legacy():
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    if digest != SOURCE_SHA256:
        raise ValueError('M1 reference changed: review the physical configuration first.')
    spec = importlib.util.spec_from_file_location('m1_reference', str(SOURCE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def readonly(value):
    result = np.array(value, dtype=float, copy=True)
    result.setflags(write=False)
    return result


class ModelData:
    """控制器不持有真实线路参数，仅持有名义值和固定不确定集合。"""

    def __init__(self, legacy):
        self.legacy = legacy
        self.n = legacy.NB - 1
        self.periods = legacy.T
        self.power_base_kw = legacy.S_BASE * 1000.0
        self.dt = legacy.DT
        self.vmin, self.vmax = legacy.V_MIN, legacy.V_MAX
        self.vlo, self.vhi = legacy.V_LO_SQ, legacy.V_HI_SQ
        self.cv, self.ccurt = legacy.C_V, legacy.C_CURT
        self.kappa = legacy.KAPPA
        self.pv = tuple(legacy.PV_BUSES)
        self.ess = tuple(legacy.ESS_BUSES)
        self.svg = tuple(legacy.SVC_BUSES)
        self.cb = tuple(legacy.CB_BUSES)
        self.ess_params = {b: dict(p) for b, p in legacy.ESS_BUSES.items()}
        self.svg_max = {b: legacy.SVC_BUSES[b] / self.power_base_kw for b in self.svg}
        self.cb_params = {b: (q / self.power_base_kw, n)
                          for b, (q, n) in legacy.CB_BUSES.items()}
        self.pm_lim = readonly(np.array(legacy.PM_LIM) / self.power_base_kw)
        self.qm_lim = readonly(np.array(legacy.QM_LIM) / self.power_base_kw)
        self.p_available = readonly(legacy.p_av[:, 1:])
        self.p_load = readonly(legacy.p_load[:, 1:])
        self.q_load = readonly(legacy.q_load[:, 1:])
        self.p_ex = readonly(self.p_available - self.p_load)
        self.q_ex = readonly(-self.q_load)

        # 行 = 原始支路顺序，列 = 物理母线 2..33（根节点母线1不在向量内）。
        A = np.zeros((self.n, self.n))
        for col in range(self.n):
            A[legacy.PATH[col + 1], col] = 1.0
        if np.linalg.matrix_rank(A) != self.n:
            raise ValueError('The topology must be a connected radial network.')
        self.A = readonly(A)
        self.controls = tuple((kind, bus) for kind, buses in (
            ('curt', self.pv), ('pess', self.ess), ('qpv', self.pv),
            ('qsvg', self.svg), ('qcb', self.cb), ('qess', self.ess))
            for bus in buses)
        self.index = {key: i for i, key in enumerate(self.controls)}
        self.nu = len(self.controls)
        bp, bq = np.zeros((self.n, self.nu)), np.zeros((self.n, self.nu))
        for j, (kind, bus) in enumerate(self.controls):
            if kind == 'curt':
                bp[bus - 2, j] = -1.0
            elif kind == 'pess':
                bp[bus - 2, j] = 1.0
            else:
                bq[bus - 2, j] = 1.0
        self.Bp, self.Bq = readonly(bp), readonly(bq)
        self.theta_nom = readonly(np.r_[legacy.r_nom, legacy.x_nom])
        self.radius = readonly(np.r_[legacy.rho_r, legacy.rho_x])
        self.theta_lo = readonly(self.theta_nom - self.radius)
        self.theta_hi = readonly(self.theta_nom + self.radius)
        if np.any(self.theta_lo <= 0) or np.any(self.theta_lo > self.theta_hi):
            raise ValueError('Invalid positive impedance box.')
        dim = 2 * self.n
        self.F = readonly(np.vstack([np.eye(dim), -np.eye(dim)]))
        self.g = readonly(np.r_[self.theta_hi, -self.theta_lo])
        abp, abq = A @ bp, A @ bq
        self.K = readonly(np.array([2 * np.vstack([
            A[:, i, None] * abp, A[:, i, None] * abq]) for i in range(self.n)]))
        self.initial_energy = readonly([
            self.ess_params[b]['soc_initial'] * self.ess_params[b]['e_capacity']
            / self.power_base_kw for b in self.ess])

    def h_external(self, t):
        if not 0 <= t < self.periods - 1:
            raise ValueError('A rolling transition requires t and t+1 data.')
        ap = self.A @ (self.p_ex[t + 1] - self.p_ex[t])
        aq = self.A @ (self.q_ex[t + 1] - self.q_ex[t])
        return 2 * np.hstack([self.A.T * ap, self.A.T * aq])

    def injections(self, period, u):
        return self.p_ex[period] + self.Bp @ u, self.q_ex[period] + self.Bq @ u

    def uncertainty_digest(self):
        return hashlib.sha256(self.F.tobytes() + self.g.tobytes()).hexdigest()

    def metadata(self):
        return {
            'source_sha256': SOURCE_SHA256, 'nodes': self.n + 1,
            'branches': self.n, 'periods': self.periods, 'dt_hours': self.dt,
            'power_base_mva': self.legacy.S_BASE, 'voltage_base_kv': self.legacy.V_BASE,
            'voltage_limits_pu': [self.vmin, self.vmax],
            'pv_kw': self.legacy.PV_BUSES, 'svg_kvar': self.legacy.SVC_BUSES,
            'cb_kvar_steps': self.legacy.CB_BUSES, 'ess': self.ess_params,
            'uncertain_branches_1based': self.legacy.UNKNOWN,
            'uncertainty_sha256': self.uncertainty_digest(),
            'nonroot_bus_order': list(range(2, self.n + 2)),
            'control_order': [list(x) for x in self.controls],
            'A_shape': list(self.A.shape), 'Bp_shape': list(self.Bp.shape),
            'Bq_shape': list(self.Bq.shape), 'K_shape': list(self.K.shape),
        }
