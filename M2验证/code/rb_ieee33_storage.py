# -*- coding: utf-8 -*-
"""
IEEE 33 节点配电网 · 含储能的鲁棒无功优化。
增加《鲁棒优化2.pdf》中的储能模型；储能参数为用户确认的示例。
本文件独立完成数据配置、模型建立与求解，不依赖其他项目脚本。
运行本文件打印结果；运行 rb_ieee33_storage_visualization.py 保存图片。

说明（对原模型的 4 处必要修正，见代码中 FIX 标注）：
  FIX-1  v_{0,t} 在原文中既不在决策向量里、也未声明取值，此处明确取常数 1.0
         （变电站母线 1.0 p.u.，电压平方形式下 v0 = V0^2 = 1.0）。
  FIX-2  松弛变量在约束 (1.10) 中漏了时段下标，此处统一为 s^+_{i,t}, s^-_{i,t}，
         与目标函数 (1.1) 一致。
  FIX-3  删除原文重复的弃光约束（原 (1.15) 与 (1.14) 完全相同）。
  FIX-4  根节点下标明确为 i=0（原 (1.6)(1.7) 标"在根节点"却用通用下标 i）。
主网交换约束按原文 §2.3 保留（否则 Q^M 无界、模型退化为"弃光 0 越限 0"）。
逆变器视在功率约束按要求暂不加，只保留功率因数约束 (1.11)。

求解器：优先 Gurobi（若已安装），否则用 scipy 自带的 HiGHS。
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

import csv

try:
    import gurobipy as gp
    HAS_GUROBI = True
except ImportError:
    HAS_GUROBI = False

# ==========================================================================
# 1. 数据
# ==========================================================================
S_BASE = 10.0          # MVA
V_BASE = 12.66         # kV
Z_BASE = V_BASE ** 2 / S_BASE          # 16.0276 ohm
T = 24                 # 时段数

# 支路 (from, to, r[ohm], x[ohm])，母线编号 1..33，顺序即拓扑序
BRANCHES = [
    (1, 2, 0.0922, 0.0470), (2, 3, 0.4930, 0.2511), (3, 4, 0.3660, 0.1864),
    (4, 5, 0.3811, 0.1941), (5, 6, 0.8190, 0.7070), (6, 7, 0.1872, 0.6188),
    (7, 8, 0.7114, 0.2351), (8, 9, 1.0300, 0.7400), (9, 10, 1.0440, 0.7400),
    (10, 11, 0.1966, 0.0650), (11, 12, 0.3744, 0.1238), (12, 13, 1.4680, 1.1550),
    (13, 14, 0.5416, 0.7129), (14, 15, 0.5910, 0.5260), (15, 16, 0.7463, 0.5450),
    (16, 17, 1.2890, 1.7210), (17, 18, 0.7320, 0.5740), (2, 19, 0.1640, 0.1565),
    (19, 20, 1.5042, 1.3554), (20, 21, 0.4095, 0.4784), (21, 22, 0.7089, 0.9373),
    (3, 23, 0.4512, 0.3083), (23, 24, 0.8980, 0.7091), (24, 25, 0.8960, 0.7011),
    (6, 26, 0.2030, 0.1034), (26, 27, 0.2842, 0.1447), (27, 28, 1.0590, 0.9337),
    (28, 29, 0.8042, 0.7006), (29, 30, 0.5075, 0.2585), (30, 31, 0.9744, 0.9630),
    (31, 32, 0.3105, 0.3619), (32, 33, 0.3410, 0.5302),
]

# 节点负荷 (bus -> (P[kW], Q[kvar]))
LOADS = {
    2: (100, 60), 3: (90, 40), 4: (120, 80), 5: (60, 30), 6: (60, 20),
    7: (200, 100), 8: (200, 100), 9: (60, 20), 10: (60, 20), 11: (45, 30),
    12: (60, 35), 13: (60, 35), 14: (120, 80), 15: (60, 10), 16: (60, 20),
    17: (60, 20), 18: (90, 40), 19: (90, 40), 20: (90, 40), 21: (90, 40),
    22: (90, 40), 23: (90, 50), 24: (420, 200), 25: (420, 200), 26: (60, 25),
    27: (60, 25), 28: (60, 20), 29: (120, 70), 30: (200, 600), 31: (150, 70),
    32: (210, 100), 33: (60, 40),
}

# 24h 归一化曲线
LOAD_SHAPE = np.array([
    0.52, 0.48, 0.46, 0.45, 0.46, 0.50, 0.58, 0.68, 0.76, 0.80, 0.82, 0.83,
    0.82, 0.82, 0.83, 0.86, 0.90, 0.95, 1.00, 0.98, 0.92, 0.84, 0.72, 0.60])
PV_SHAPE = np.array([
    0.00, 0.00, 0.00, 0.00, 0.00, 0.02, 0.10, 0.22, 0.38, 0.55, 0.71, 0.85,
    0.94, 0.97, 0.94, 0.86, 0.73, 0.56, 0.36, 0.17, 0.04, 0.00, 0.00, 0.00])

# 光伏 (bus -> 装机 kW)；逆变器最小功率因数 0.95
# 高渗透率场景；以下容量与原文件一致，总装机为10000 kW。
PV_BUSES = {18: 3000.0, 22: 2000.0, 25: 2000.0, 33: 3000.0}
PF_MIN = 0.95
KAPPA = float(np.tan(np.arccos(PF_MIN)))

# SVG (bus -> ±kvar)
SVC_BUSES = {18: 600.0, 33: 600.0}

# 并联电容器 (bus -> (每组 kvar, 最大组数))
CB_BUSES = {6: (150.0, 4), 12: (150.0, 4), 24: (150.0, 4), 30: (150.0, 4)}

# 未知线路：4 条，参数在名义值 ±30% 内浮动（1-based 支路序号）
UNKNOWN = {5: 0.30, 12: 0.30, 16: 0.30, 29: 0.30}

# 主网交换限值 (kW / kvar)
PM_LIM = (-20000.0, 20000.0)
QM_LIM = (-1500.0, 1500.0)

# 电压限值与权重
V_MIN, V_MAX = 0.93, 1.05
V_LO_SQ, V_HI_SQ = V_MIN ** 2, V_MAX ** 2     # [0.8649, 1.1025]
V0_SQ = 1.0                                    # FIX-1：根节点电压平方，已知常数
C_V = 100.0                                    # 电压越限惩罚
C_CURT = 1.0                                   # 弃光惩罚

# 新增储能配置：每个物理节点一组参数，功率用kW/kvar，电量用kWh。
DT = 1.0                                      # 每个时段1小时
ESS_BUSES = {
    18: {"p_ch_max": 1000.0, "p_dis_max": 1000.0, "e_capacity": 2000.0,
         "q_max": 600.0, "eta_ch": 0.95, "eta_dis": 0.95,
         "soc_min": 0.20, "soc_max": 0.90, "soc_initial": 0.50},
    #33: {"p_ch_max": 1000.0, "p_dis_max": 1000.0, "e_capacity": 2000.0,
     #    "q_max": 600.0, "eta_ch": 0.95, "eta_dis": 0.95,
      #   "soc_min": 0.20, "soc_max": 0.90, "soc_initial": 0.50},
}


# ==========================================================================
# 2. 拓扑预处理
# ==========================================================================
NB = len(BRANCHES) + 1        # 33 个节点，0-based，0 号为根
NBR = len(BRANCHES)           # 32 条支路
br_from = np.array([b[0] - 1 for b in BRANCHES])
br_to = np.array([b[1] - 1 for b in BRANCHES])
r_nom = np.array([b[2] for b in BRANCHES]) / Z_BASE
x_nom = np.array([b[3] for b in BRANCHES]) / Z_BASE

# 节点 -> 上游支路
edge_of_bus = np.full(NB, -1, dtype=int)
for k in range(NBR):
    edge_of_bus[br_to[k]] = k
# 节点 -> 下游支路
children_br = [[] for _ in range(NB)]
for k in range(NBR):
    children_br[br_from[k]].append(k)
# 节点 -> 根路径支路集合
PATH = [[] for _ in range(NB)]
for j in range(1, NB):
    seq, cur = [], j
    while cur != 0:
        k = edge_of_bus[cur]
        seq.append(k)
        cur = br_from[k]
    PATH[j] = seq[::-1]

rho_r = np.zeros(NBR)
rho_x = np.zeros(NBR)
for k, d in UNKNOWN.items():
    rho_r[k - 1] = d * r_nom[k - 1]
    rho_x[k - 1] = d * x_nom[k - 1]
UNK = [k for k in range(NBR) if rho_r[k] > 0 or rho_x[k] > 0]

pv_idx = {b - 1: cap / 1000.0 / S_BASE for b, cap in PV_BUSES.items()}
svc_idx = {b - 1: q / 1000.0 / S_BASE for b, q in SVC_BUSES.items()}
cb_idx = {b - 1: (q / 1000.0 / S_BASE, nmax) for b, (q, nmax) in CB_BUSES.items()}
ess_idx = {bus - 1: config for bus, config in ESS_BUSES.items()}

p_load = np.zeros((T, NB))
q_load = np.zeros((T, NB))
for b, (pk, qk) in LOADS.items():
    p_load[:, b - 1] = pk / 1000.0 / S_BASE * LOAD_SHAPE
    q_load[:, b - 1] = qk / 1000.0 / S_BASE * LOAD_SHAPE

p_av = np.zeros((T, NB))
for i, cap in pv_idx.items():
    p_av[:, i] = PV_SHAPE * cap


# ==========================================================================
# 3. 建模
# ==========================================================================
class Builder:
    def __init__(self):
        self.names, self.lb, self.ub, self.integrality = [], [], [], []
        self.obj, self.rows = {}, []

    def var(self, name, lb=0.0, ub=np.inf, integer=False):
        '''
        登记一个决策变量；
        name      变量名字符串；
        lb        变量下界，默认为0；
        up        变量上界，默认正无穷；
        integer   整数变量标记；
        i         变量编号索引；
        '''
        i = len(self.names)
        self.names.append(name); self.lb.append(lb); self.ub.append(ub)
        self.integrality.append(1 if integer else 0)
        return i

    def con(self, coefs, lo=-np.inf, hi=np.inf):
        '''
        登记一条线性约束；
        cofes  字典，变量的线性系数；
        lb     约束下界，默认负无穷；
        up     约束上界，默认正无穷；

        '''
        self.rows.append((coefs, lo, hi))

    def solve(self):
        '''
        组装稀疏矩阵并调用求解器
        '''
        nr, nc = len(self.rows), len(self.names)
        rr, cc, vv = [], [], []
        lo, hi = np.empty(nr), np.empty(nr)
        for r, (c, l, h) in enumerate(self.rows):
            lo[r], hi[r] = l, h
            for j, v in c.items():
                if v != 0.0:
                    rr.append(r); cc.append(j); vv.append(v)
        A = coo_matrix((vv, (rr, cc)), shape=(nr, nc)).tocsc()
        c = np.zeros(nc)
        for j, v in self.obj.items():
            c[j] = v

        if HAS_GUROBI:
            m = gp.Model("rb_ieee33_storage")
            m.Params.OutputFlag = 1
            integer = np.array(self.integrality) == 1
            binary = integer & (np.array(self.lb) == 0) & (np.array(self.ub) == 1)
            x = m.addMVar(nc, lb=np.array(self.lb), ub=np.array(self.ub),
                          vtype=np.where(binary, "B", np.where(integer, "I", "C")))
            m.setObjective(c @ x, gp.GRB.MINIMIZE)
            # 只传入有限的约束边界，避免把无穷大变成1e100的右端项。
            equal = np.isfinite(lo) & np.isfinite(hi) & (lo == hi)
            lower = np.isfinite(lo) & ~equal
            upper = np.isfinite(hi) & ~equal
            if equal.any():
                m.addConstr(A[equal] @ x == lo[equal])
            if lower.any():
                m.addConstr(A[lower] @ x >= lo[lower])
            if upper.any():
                m.addConstr(A[upper] @ x <= hi[upper])
            m.optimize()
            status = "optimal" if m.Status == 2 else f"status {m.Status}"
            xs = x.X if m.Status == 2 else None
            obj = m.ObjVal if m.Status == 2 else None
            m.dispose()
        else:
            res = milp(c=c, constraints=[LinearConstraint(A, lo, hi)],
                       integrality=np.array(self.integrality),
                       bounds=Bounds(np.array(self.lb), np.array(self.ub)),
                       options={"disp": False})
            status = "optimal" if res.status == 0 else f"status {res.status}: {res.message}"
            xs = res.x if res.status == 0 else None
            obj = res.fun if res.status == 0 else None
        return status, obj, xs


b = Builder()
var_idx = {}

# --- 决策变量 ---
# 支路潮流
for k in range(NBR):
    for t in range(T):
        var_idx[("P", k, t)] = b.var(f"P[{k},{t}]", -5, 5)
        var_idx[("Q", k, t)] = b.var(f"Q[{k},{t}]", -5, 5)

for i in pv_idx:
    for t in range(T):
        var_idx[("curt", i, t)] = b.var(f"curt[{i},{t}]", 0, p_av[t, i])
        var_idx[("qpv", i, t)] = b.var(f"qpv[{i},{t}]", -1, 1)

for i in svc_idx:
    for t in range(T):
        var_idx[("qsvc", i, t)] = b.var(f"qsvc[{i},{t}]", -svc_idx[i], svc_idx[i])

for i, (qstep, nmax) in cb_idx.items():
    for t in range(T):
        var_idx[("gam", i, t)] = b.var(f"gam[{i},{t}]", 0, nmax, integer=True)

for t in range(T):
    var_idx[("PM", t)] = b.var(f"PM[{t}]", PM_LIM[0] / 1000 / S_BASE, PM_LIM[1] / 1000 / S_BASE)
    var_idx[("QM", t)] = b.var(f"QM[{t}]", QM_LIM[0] / 1000 / S_BASE, QM_LIM[1] / 1000 / S_BASE)

for i in range(NB):
    for t in range(T):
        var_idx[("sp", i, t)] = b.var(f"sp[{i},{t}]", 0, 1.0)     # s^+
        var_idx[("sm", i, t)] = b.var(f"sm[{i},{t}]", 0, 1.0)     # s^-
        var_idx[("v0", i, t)] = b.var(f"v0[{i},{t}]", -2, 3)
        var_idx[("Psi", i, t)] = b.var(f"Psi[{i},{t}]", 0, 2)

for k in UNK:
    for t in range(T):
        var_idx[("uP", k, t)] = b.var(f"uP[{k},{t}]", 0, 5)
        var_idx[("uQ", k, t)] = b.var(f"uQ[{k},{t}]", 0, 5)

# 储能
for i, config in ess_idx.items():
    # 电量除以功率基值后单位为p.u.·h，与功率p.u.乘小时一致。
    power_base = S_BASE * 1000.0
    pc = config["p_ch_max"] / power_base
    pd = config["p_dis_max"] / power_base
    qc = config["q_max"] / power_base
    capacity = config["e_capacity"] / power_base
    for t in range(T):
        var_idx[("pch", i, t)] = b.var(f"pch[{i},{t}]", 0, pc)
        var_idx[("pdis", i, t)] = b.var(f"pdis[{i},{t}]", 0, pd)
        var_idx[("pess", i, t)] = b.var(f"pess[{i},{t}]", -pc, pd)
        var_idx[("qess", i, t)] = b.var(f"qess[{i},{t}]", -qc, qc)
        var_idx[("zch", i, t)] = b.var(f"zch[{i},{t}]", 0, 1, integer=True)
        var_idx[("zdis", i, t)] = b.var(f"zdis[{i},{t}]", 0, 1, integer=True)
    # 24次充放电对应25个电量状态：0为初始，24为日末。
    for t in range(T + 1):
        var_idx[("E", i, t)] = b.var(f"E[{i},{t}]",
                               config["soc_min"] * capacity,
                               config["soc_max"] * capacity)

# 节点净注入 p_{i,t}, q_{i,t}（线性表达式） ---
def p_inj(i, t):
    """返回 (coefs, const)，表示 p_{i,t}"""
    c = {var_idx[("curt", i, t)]: -1.0} if i in pv_idx else {}
    const = -p_load[t, i]
    if i in pv_idx:
        const += p_av[t, i]
    if i in ess_idx:
        c[var_idx[("pess", i, t)]] = 1.0             # 放电正注入，充电负注入
    if i == 0:                                   # FIX-4：根节点加入主网项
        c[var_idx[("PM", t)]] = 1.0
    return c, const


def q_inj(i, t):
    c = {}
    const = -q_load[t, i]
    if i in pv_idx:
        c[var_idx[("qpv", i, t)]] = 1.0
    if i in svc_idx:
        c[var_idx[("qsvc", i, t)]] = 1.0
    if i in cb_idx:
        qstep = cb_idx[i][0]
        c[var_idx[("gam", i, t)]] = qstep
    if i in ess_idx:
        c[var_idx[("qess", i, t)]] = 1.0
    if i == 0:
        c[var_idx[("QM", t)]] = 1.0
    return c, const


# --- (1.2)(1.3) 潮流约束 ---
for t in range(T):
    for i in range(NB):
        # sum_{m: i->m} P_{im,t} - p_{i,t} = P_{ji,t}  （根节点左侧无 P_{ji}）
        cp = {}
        for m in children_br[i]:
            cp[var_idx[("P", m, t)]] = cp.get(var_idx[("P", m, t)], 0.0) + 1.0
        cq = {}
        for m in children_br[i]:
            cq[var_idx[("Q", m, t)]] = cq.get(var_idx[("Q", m, t)], 0.0) + 1.0
        pp, pconst = p_inj(i, t)
        qq, qconst = q_inj(i, t)
        for k2, v in pp.items():
            cp[k2] = cp.get(k2, 0.0) - v
        for k2, v in qq.items():
            cq[k2] = cq.get(k2, 0.0) - v
        if i == 0:
            b.con(cp, pconst, pconst)
            b.con(cq, qconst, qconst)
        else:
            ke = edge_of_bus[i]
            cp[var_idx[("P", ke, t)]] = cp.get(var_idx[("P", ke, t)], 0.0) - 1.0
            cq[var_idx[("Q", ke, t)]] = cq.get(var_idx[("Q", ke, t)], 0.0) - 1.0
            b.con(cp, pconst, pconst)
            b.con(cq, qconst, qconst)

# 绝对值辅助变量
for k in UNK:
    for t in range(T):
        b.con({var_idx[("uP", k, t)]: 1.0, var_idx[("P", k, t)]: -1.0}, 0.0, np.inf)
        b.con({var_idx[("uP", k, t)]: 1.0, var_idx[("P", k, t)]: 1.0}, 0.0, np.inf)
        b.con({var_idx[("uQ", k, t)]: 1.0, var_idx[("Q", k, t)]: -1.0}, 0.0, np.inf)
        b.con({var_idx[("uQ", k, t)]: 1.0, var_idx[("Q", k, t)]: 1.0}, 0.0, np.inf)

# 名义电压 与 鲁棒电压偏差
for i in range(NB):
    for t in range(T):
        # (1.8)  v0_{i,t} = V0^2 - 2*sum_{k in P_i}(r_k P_{k,t} + x_k Q_{k,t})
        #       移项后 P、Q 的系数为 +2r、+2x
        c = {var_idx[("v0", i, t)]: 1.0}
        for k in PATH[i]:
            c[var_idx[("P", k, t)]] = c.get(var_idx[("P", k, t)], 0.0) + 2.0 * r_nom[k]
            c[var_idx[("Q", k, t)]] = c.get(var_idx[("Q", k, t)], 0.0) + 2.0 * x_nom[k]
        b.con(c, V0_SQ, V0_SQ)

        # (1.9)  Psi_{i,t} = sum_{k in P_i ∩ E_u}(rho^r_k u^P_{k,t} + rho^x_k u^Q_{k,t})
        cp = {var_idx[("Psi", i, t)]: 1.0}
        for k in PATH[i]:
            if k in UNK:
                cp[var_idx[("uP", k, t)]] = cp.get(var_idx[("uP", k, t)], 0.0) - rho_r[k]
                cp[var_idx[("uQ", k, t)]] = cp.get(var_idx[("uQ", k, t)], 0.0) - rho_x[k]
        b.con(cp, 0.0, 0.0)

# 电压软约束
for i in range(1, NB):                            # 根节点电压恒为 1.0，无需约束
    for t in range(T):
        # v0 - 2Psi + s^- >= v_lo
        c1 = {var_idx[("v0", i, t)]: 1.0, var_idx[("Psi", i, t)]: -2.0, var_idx[("sm", i, t)]: 1.0}
        b.con(c1, V_LO_SQ, np.inf)
        # v0 + 2Psi - s^+ <= v_hi
        c2 = {var_idx[("v0", i, t)]: 1.0, var_idx[("Psi", i, t)]: 2.0, var_idx[("sp", i, t)]: -1.0}
        b.con(c2, -np.inf, V_HI_SQ)

# 逆变器功率因数约束
for i in pv_idx:
    for t in range(T):
        # -kappa*(p_av - curt) <= qpv <= kappa*(p_av - curt)
        c1 = {var_idx[("qpv", i, t)]: 1.0, var_idx[("curt", i, t)]: KAPPA}
        b.con(c1, -np.inf, KAPPA * p_av[t, i])
        c2 = {var_idx[("qpv", i, t)]: -1.0, var_idx[("curt", i, t)]: KAPPA}
        b.con(c2, -np.inf, KAPPA * p_av[t, i])

# 储能约束
for i, config in ess_idx.items():
    for t in range(T):
        # 充放电功率受二进制状态控制，两个状态不能同时取1。
        b.con({var_idx[("pch", i, t)]: 1,
               var_idx[("zch", i, t)]: -config["p_ch_max"] / (1000 * S_BASE)}, hi=0)
        b.con({var_idx[("pdis", i, t)]: 1,
               var_idx[("zdis", i, t)]: -config["p_dis_max"] / (1000 * S_BASE)}, hi=0)
        b.con({var_idx[("zch", i, t)]: 1, var_idx[("zdis", i, t)]: 1}, hi=1)
        # P_ESS = P_dis - P_ch；文档的P^h按上下文解释为P^ch。
        b.con({var_idx[("pess", i, t)]: 1, var_idx[("pdis", i, t)]: -1,
               var_idx[("pch", i, t)]: 1}, 0, 0)
        # E[t+1] = E[t] + eta_ch*P_ch*DT - P_dis*DT/eta_dis。
        b.con({var_idx[("E", i, t + 1)]: 1, var_idx[("E", i, t)]: -1,
               var_idx[("pch", i, t)]: -config["eta_ch"] * DT,
               var_idx[("pdis", i, t)]: DT / config["eta_dis"]}, 0, 0)
    initial = config["e_capacity"] * config["soc_initial"] / (1000 * S_BASE)
    b.con({var_idx[("E", i, 0)]: 1}, initial, initial)
# E与Q_ESS上下限已在变量定义时设置。按文档不增加日末电量回归条件，
# 也不增加储能有功/无功视在容量耦合约束或充放电成本。

# --- 目标函数 (1.1)：FIX-2 松弛变量带时段下标 ---
obj = {}
for i in range(NB):
    for t in range(T):
        obj[var_idx[("sp", i, t)]] = obj.get(var_idx[("sp", i, t)], 0.0) + C_V
        obj[var_idx[("sm", i, t)]] = obj.get(var_idx[("sm", i, t)], 0.0) + C_V
for i in pv_idx:
    for t in range(T):
        obj[var_idx[("curt", i, t)]] = obj.get(var_idx[("curt", i, t)], 0.0) + C_CURT
b.obj = obj


# ==========================================================================
# 4. 求解
# ==========================================================================
def extract_results(x):
    """按[时段,节点]提取结果，只保留在内存中，供打印和绘图使用。"""
    kinds = ("curt", "qpv", "qsvc", "gam", "sp", "sm", "v0", "Psi",
             "pch", "pdis", "pess", "qess", "zch", "zdis")
    values = {name: np.zeros((T, NB)) for name in kinds}
    values["PM"] = np.zeros(T)
    values["QM"] = np.zeros(T)
    values["E"] = np.zeros((T + 1, NB))
    values["P"] = np.zeros((T, NBR))
    values["Q"] = np.zeros((T, NBR))
    for key, index in var_idx.items():
        if key[0] not in values:
            continue
        if len(key) == 2:
            values[key[0]][key[1]] = x[index]
        else:
            values[key[0]][key[2], key[1]] = x[index]
    # uP/uQ只有绝对值下界，未必取等号；用实际潮流重算精确盒集偏差。
    for i in range(NB):
        path = PATH[i]
        values["Psi"][:, i] = (rho_r[path] * abs(values["P"][:, path])
                                + rho_x[path] * abs(values["Q"][:, path])).sum(axis=1)
    return values


def export_all_vars_csv(x, filename="all_variables.csv"):
    """
    把全部决策变量的解导出为 CSV。
    列：变量种类, 空间类型, 空间索引, 时段, 变量下标, 数值
      - 空间类型：'node'（节点 i）/ 'branch'（支路 k）/ 'none'（无空间维度，如 PM/QM）
      - 空间索引：节点或支路的 0-based 下标；'none' 型留空
      - 时段：0..T-1；E 变量为 0..T
    """
    # 种类 -> 空间类型
    node_kinds = {"curt", "qpv", "qsvc", "gam", "sp", "sm", "v0", "Psi",
                  "pch", "pdis", "pess", "qess", "zch", "zdis", "E"}
    branch_kinds = {"P", "Q", "uP", "uQ"}
    time_only_kinds = {"PM", "QM"}

    rows = []
    for key, index in var_idx.items():
        kind = key[0]
        val = x[index]

        if len(key) == 2:                      # ("PM", t)
            if kind not in time_only_kinds:
                continue
            t = key[1]
            rows.append((kind, "none", "", t, index, val))

        elif len(key) == 3:                    # (kind, i_or_k, t)
            if kind in node_kinds:
                space_type = "node"
            elif kind in branch_kinds:
                space_type = "branch"
            else:
                space_type = "unknown"         # 防御：未登记的种类
            space_idx = key[1]
            t = key[2]
            rows.append((kind, space_type, space_idx, t, index, val))

    # 排序：先按种类，再按空间索引，再按时段，便于阅读
    rows.sort(key=lambda r: (r[0], str(r[2]), r[3]))

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["kind", "space_type", "space_index", "time",
                         "var_index", "value"])
        writer.writerows(rows)

    print(f"已导出 {len(rows)} 个变量到 {filename}")
    return filename


def main():
    print(f"节点 {NB}，支路 {NBR}，时段 {T}；未知支路配置 {UNKNOWN}")
    print(f"C_V={C_V}，C_CURT={C_CURT}；储能节点 {list(ESS_BUSES)}")
    print(f"变量 {len(b.names)}，约束 {len(b.rows)}，整数变量 {sum(b.integrality)}")
    status, objective, x = b.solve()
    print(f"求解器: {'Gurobi' if HAS_GUROBI else 'HiGHS'}；状态: {status}")
    if x is None:
        raise RuntimeError(f"模型没有得到最优解：{status}")
    values = extract_results(x)
    export_all_vars_csv(x, "all_variables.csv")
    scale = S_BASE * 1000.0
    print(f"目标值 = {objective:.8f}")
    print(f"光伏可用电量 = {p_av.sum() * scale * DT:.2f} kWh")
    print(f"弃光电量 = {values['curt'].sum() * scale * DT:.2f} kWh")
    print(f"上越限松弛 = {values['sp'].sum():.8f} p.u.^2")
    print(f"下越限松弛 = {values['sm'].sum():.8f} p.u.^2")
    lower_sq = values["v0"][:, 1:] - 2 * values["Psi"][:, 1:]
    upper_sq = values["v0"][:, 1:] + 2 * values["Psi"][:, 1:]
    print(f"最坏电压范围 = [{np.sqrt(max(lower_sq.min(), 0)):.6f}, "
          f"{np.sqrt(max(upper_sq.max(), 0)):.6f}] p.u.")
    for bus in ESS_BUSES:
        i = bus - 1
        print(f"储能节点{bus}：充电 {values['pch'][:, i].sum() * scale * DT:.2f} kWh，"
              f"放电 {values['pdis'][:, i].sum() * scale * DT:.2f} kWh，"
              f"初始/日末电量 {values['E'][0, i] * scale:.2f}/"
              f"{values['E'][-1, i] * scale:.2f} kWh")


if __name__ == "__main__":
    main()
