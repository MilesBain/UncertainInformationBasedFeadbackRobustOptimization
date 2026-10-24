"""独立真实电网：原始阻抗、恒PQ设备注入、非线性交流潮流。"""
from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
import numpy as np
from pypower.runpf import runpf
from pypower.ppoption import ppoption


@dataclass
class PowerFlowResult:
    v_complex: np.ndarray
    voltage_pu: np.ndarray
    voltage_sq: np.ndarray
    p_injected: np.ndarray
    q_injected: np.ndarray
    grid_p_pu: float
    grid_q_pu: float
    branch_p_from: np.ndarray
    branch_q_from: np.ndarray
    branch_p_to: np.ndarray
    branch_q_to: np.ndarray
    loss_p_pu: float
    loss_q_pu: float
    balance_residual_pu: float
    mapping_residual_pu: float
    runtime_s: float


class ACPlant:
    """此对象单独持有theta*，控制器求解函数无需访问本对象。"""

    def __init__(self, legacy):
        self.source = legacy
        # M1没有单独的AC网络/扰动真值，故真实值取原BRANCHES，不随机改动。
        self.r_true = np.array(legacy.r_nom, copy=True)
        self.x_true = np.array(legacy.x_nom, copy=True)
        self.r_true.setflags(write=False)
        self.x_true.setflags(write=False)

    def check_box_membership(self, data):
        theta = np.r_[self.r_true, self.x_true]
        if np.any(theta < data.theta_lo) or np.any(theta > data.theta_hi):
            raise ValueError('The fixed true impedances lie outside U0.')
        return True

    def _physical_injections(self, data, period, dispatch):
        """逐设备写入；不使用Bp/Bq计算，供独立检验映射符号。"""
        old = self.source
        p = -old.p_load[period].copy()
        q = -old.q_load[period].copy()
        for bus in old.PV_BUSES:
            p[bus - 1] += old.p_av[period, bus - 1] - dispatch.u[data.index['curt', bus]]
            q[bus - 1] += dispatch.u[data.index['qpv', bus]]
        for j, bus in enumerate(old.ESS_BUSES):
            p[bus - 1] += dispatch.pdis[j] - dispatch.pch[j]
            q[bus - 1] += dispatch.u[data.index['qess', bus]]
        for bus in old.SVC_BUSES:
            q[bus - 1] += dispatch.u[data.index['qsvg', bus]]
        for j, bus in enumerate(old.CB_BUSES):
            # 按原模型Q_CB=档位*额定每档无功，为恒Q；不擅自改为V²相关导纳。
            q[bus - 1] += dispatch.gamma[j] * old.CB_BUSES[bus][0] / data.power_base_kw
        return p, q

    def run(self, data, period, dispatch):
        old = self.source
        self.check_box_membership(data)
        p, q = self._physical_injections(data, period, dispatch)
        mapped_p, mapped_q = data.injections(period, dispatch.u)
        mapping_error = max(np.max(np.abs(p[1:] - mapped_p)), np.max(np.abs(q[1:] - mapped_q)))
        if mapping_error > 1e-7:
            raise RuntimeError('Device setpoints disagree with controller injection mapping.')
        # PYPOWER/MATPOWER case v2; PD/QD是消耗功率，故使用负注入。
        bus = np.zeros((old.NB, 13))
        bus[:, 0] = np.arange(1, old.NB + 1)
        bus[:, 1] = 1
        bus[0, 1] = 3
        bus[:, 2] = -p * old.S_BASE
        bus[:, 3] = -q * old.S_BASE
        bus[:, 6] = 1
        bus[:, 7] = 1
        bus[:, 9] = old.V_BASE
        bus[:, 10] = 1
        bus[:, 11], bus[:, 12] = old.V_MAX, old.V_MIN
        branch = np.zeros((old.NBR, 13))
        branch[:, 0] = np.array(old.br_from) + 1
        branch[:, 1] = np.array(old.br_to) + 1
        branch[:, 2], branch[:, 3] = self.r_true, self.x_true
        branch[:, 10] = 1
        branch[:, 11], branch[:, 12] = -360, 360
        # 支路充电电纳、变比/相移均为零（原网络没有这些元件）。容量0表示不设置。
        gen = np.zeros((1, 21))
        gen[0, 0] = 1
        gen[0, 3], gen[0, 4] = old.QM_LIM[1] / 1000, old.QM_LIM[0] / 1000
        gen[0, 5:8] = [np.sqrt(old.V0_SQ), old.S_BASE, 1]
        gen[0, 8], gen[0, 9] = old.PM_LIM[1] / 1000, old.PM_LIM[0] / 1000
        case = {'version': '2', 'baseMVA': old.S_BASE, 'bus': bus, 'gen': gen, 'branch': branch}
        options = ppoption(PF_ALG=1, PF_TOL=1e-10, PF_MAX_IT=50, VERBOSE=0, OUT_ALL=0)
        started = perf_counter()
        result, success = runpf(case, options)
        runtime = perf_counter() - started
        if not success:
            raise RuntimeError('AC power flow failed at period %d; state not advanced.' % period)
        vm = result['bus'][:, 7]
        vc = vm * np.exp(1j * np.deg2rad(result['bus'][:, 8]))
        if not np.isfinite(vc).all() or np.any(vm <= 0) or abs(vm[0] - 1) > 1e-10:
            raise RuntimeError('Invalid AC voltage solution.')
        # 独立按原线路复阻抗计算节点电流与S=V*conj(I)，验证AC功率平衡。
        current = np.zeros(old.NB, dtype=complex)
        for k, (fr, to) in enumerate(zip(old.br_from, old.br_to)):
            ik = (vc[fr] - vc[to]) / complex(self.r_true[k], self.x_true[k])
            current[fr] += ik
            current[to] -= ik
        s = vc * np.conj(current)
        balance = float(max(np.max(np.abs(s.real[1:] - p[1:])),
                            np.max(np.abs(s.imag[1:] - q[1:]))))
        if balance > 1e-8:
            raise RuntimeError('Independent AC nodal power balance check failed.')
        pf, qf, pt, qt = (result['branch'][:, col] / old.S_BASE for col in (13, 14, 15, 16))
        return PowerFlowResult(vc, vm.copy(), vm[1:] ** 2, p[1:], q[1:],
                               float(result['gen'][0, 1] / old.S_BASE),
                               float(result['gen'][0, 2] / old.S_BASE),
                               pf, qf, pt, qt, float((pf + pt).sum()), float((qf + qt).sum()),
                               balance, float(mapping_error), runtime)


def backward_forward_check(legacy, p, q, r, x, tolerance=1e-12):
    """独立径向复电流前推回代AC求解，仅用于验算PYPOWER结果。"""
    voltage = np.ones(legacy.NB, dtype=complex)
    s_injected = np.r_[0j, p + 1j * q]
    edge_order = sorted(range(legacy.NBR), key=lambda k: len(legacy.PATH[legacy.br_to[k]]))
    for iteration in range(1, 2001):
        previous = voltage.copy()
        downstream = np.conj(-s_injected / voltage)
        current = np.zeros(legacy.NBR, dtype=complex)
        for k in reversed(edge_order):
            fr, to = legacy.br_from[k], legacy.br_to[k]
            current[k] = downstream[to]
            downstream[fr] += current[k]
        voltage[0] = 1
        for k in edge_order:
            fr, to = legacy.br_from[k], legacy.br_to[k]
            voltage[to] = voltage[fr] - complex(r[k], x[k]) * current[k]
        if np.max(np.abs(voltage - previous)) < tolerance:
            return voltage, iteration
    raise RuntimeError('Independent backward-forward AC check did not converge.')
