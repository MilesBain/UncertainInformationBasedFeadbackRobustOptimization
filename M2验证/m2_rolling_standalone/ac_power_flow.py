"""独立的真实物理环境：执行设备动作，调用PYPOWER非线性AC潮流。

这里不用优化器的LinDistFlow方程生成“真实电压”。设备逐项写入PQ注入，
再与Bp/Bq映射结果核对；电容器按给定恒定无功建模，不改成V²相关并联电纳。
"""
from time import perf_counter
import numpy as np
from pypower.api import ppoption, runpf
import config
from states import PowerFlowResult


def physical_injections(data, period, action):
    """按设备含义重构净注入，不使用Bp/Bq；返回非根节点p、q。"""
    p = -data.p_load[period].copy()
    q = -data.q_load[period].copy()
    for bus in data.pv_buses:
        node = data.node_index[bus]
        p[node] += data.pv_available[period, node] - action.control[data.control_index['curt', bus]]
        q[node] += action.control[data.control_index['qpv', bus]]
    for bus in data.svg_buses:
        q[data.node_index[bus]] += action.control[data.control_index['qsvg', bus]]
    for index, bus in enumerate(data.capacitor_buses):
        q[data.node_index[bus]] += data.capacitors[bus][0] * action.capacitor_steps[index]
    for index, bus in enumerate(data.storage_buses):
        p[data.node_index[bus]] += action.discharge_power[index] - action.charge_power[index]
        q[data.node_index[bus]] += action.control[data.control_index['qess', bus]]
    return p, q


def true_impedance(data):
    """AC真值只由本项目config给出，不读取或更新控制器的不确定集合。"""
    r = np.asarray(config.TRUE_RESISTANCE_OHM) / data.base_ohm
    x = np.asarray(config.TRUE_REACTANCE_OHM) / data.base_ohm
    if r.shape != (data.n,) or x.shape != (data.n,) or not np.isfinite(r + x).all():
        raise ValueError('真实阻抗的长度或数值不合法。')
    if np.any(r <= 0) or np.any(x <= 0):
        raise ValueError('本模型要求真实线路电阻、电抗为正。')
    return r + 1j * x


def build_ac_case(data, p, q):
    """建立PYPOWER case字典，优化标幺功率转换为MW/Mvar。"""
    bus = np.zeros((len(data.network.buses), 13))
    bus[:, 0] = data.network.buses          # BUS_I：物理母线编号
    bus[:, 1] = 1                         # PQ节点
    bus[data.network.root_bus - 1, 1] = 3 # REF节点
    for index, number in enumerate(data.network.nonroot_buses):
        bus[number - 1, 2] = -p[index] * data.base_mva  # PD：正号表示负荷
        bus[number - 1, 3] = -q[index] * data.base_mva  # QD
    bus[:, 6] = 1                         # area
    bus[:, 7] = data.root_voltage         # 初始电压幅值
    bus[:, 9] = data.base_kv
    bus[:, 10] = 1                        # zone
    bus[:, 11] = data.voltage_max
    bus[:, 12] = data.voltage_min

    branch = np.zeros((data.n, 13))
    branch[:, 0] = data.network.branch_from
    branch[:, 1] = data.network.branch_to
    impedance = true_impedance(data)
    branch[:, 2] = impedance.real
    branch[:, 3] = impedance.imag
    branch[:, 10] = 1                     # BR_STATUS
    branch[:, 11] = -360
    branch[:, 12] = 360

    generator = np.zeros((1, 21))
    generator[0, 0] = data.network.root_bus
    generator[0, 3] = data.grid_q_limits[1] * data.base_mva
    generator[0, 4] = data.grid_q_limits[0] * data.base_mva
    generator[0, 5] = data.root_voltage
    generator[0, 6] = data.base_mva
    generator[0, 7] = 1
    generator[0, 8] = data.grid_p_limits[1] * data.base_mva
    generator[0, 9] = data.grid_p_limits[0] * data.base_mva
    return {'version': '2', 'baseMVA': data.base_mva, 'bus': bus,
            'branch': branch, 'gen': generator}


def run_ac(data, period, action):
    """执行一个时段。仅潮流收敛且映射/功率平衡通过时返回结果。"""
    started = perf_counter()
    p, q = physical_injections(data, period, action)
    mapped_p, mapped_q = data.injections(period, action.control)
    mapping_error = max(np.max(np.abs(p - mapped_p)), np.max(np.abs(q - mapped_q)))
    if mapping_error > 1e-7:
        raise RuntimeError('设备动作到AC注入的映射不一致。')
    options = ppoption(VERBOSE=0, OUT_ALL=0, PF_ALG=1, PF_TOL=1e-10,
                       PF_MAX_IT=50, ENFORCE_Q_LIMS=False)
    solved, success = runpf(build_ac_case(data, p, q), options)
    if not success:
        raise RuntimeError('时段%d的AC潮流不收敛，停止状态推进。' % period)
    voltage = solved['bus'][:, 7] * np.exp(1j * np.deg2rad(solved['bus'][:, 8]))
    if not np.isfinite(voltage).all() or np.any(np.abs(voltage) <= 0):
        raise RuntimeError('AC潮流返回了非法电压。')

    # 直接用复电压和欧姆定律复核各节点注入，独立于PYPOWER输出的支路功率。
    outgoing_current = np.zeros(len(data.network.buses), dtype=complex)
    impedance = true_impedance(data)
    for index in range(data.n):
        start = data.network.branch_from[index] - 1
        end = data.network.branch_to[index] - 1
        current = (voltage[start] - voltage[end]) / impedance[index]
        outgoing_current[start] += current
        outgoing_current[end] -= current
    calculated_injection = voltage * np.conj(outgoing_current)
    nonroot = np.array(data.network.nonroot_buses) - 1
    balance_error = float(np.max(np.abs(calculated_injection[nonroot] - (p + 1j * q))))
    if balance_error > 1e-7:
        raise RuntimeError('AC节点功率平衡复核失败。')
    flows = solved['branch'][:, 13:17] / data.base_mva
    return PowerFlowResult(voltage, np.abs(voltage), np.abs(voltage[nonroot]) ** 2,
                           p, q, float(solved['gen'][0, 1] / data.base_mva),
                           float(solved['gen'][0, 2] / data.base_mva),
                           float((flows[:, 0] + flows[:, 2]).sum()),
                           float((flows[:, 1] + flows[:, 3]).sum()),
                           flows[:, 0], flows[:, 1], flows[:, 2], flows[:, 3],
                           float(mapping_error), balance_error, perf_counter() - started)


def backward_forward_sweep(data, p, q):
    """独立前推回代潮流，仅用于核验PYPOWER；不参与优化或反馈修正。"""
    voltage = np.full(len(data.network.buses), complex(data.root_voltage))
    injection = np.zeros(len(voltage), dtype=complex)
    injection[np.array(data.network.nonroot_buses) - 1] = p + 1j * q
    impedance = true_impedance(data)
    for iteration in range(2000):
        previous = voltage.copy()
        # 先从末端累加流向负荷的电流，再从根节点更新电压。
        bus_current = np.conj(-injection / voltage)
        branch_current = np.zeros(data.n, dtype=complex)
        for branch in reversed(data.network.forward_order):
            start = data.network.branch_from[branch] - 1
            end = data.network.branch_to[branch] - 1
            branch_current[branch] = bus_current[end]
            bus_current[start] += bus_current[end]
        for branch in data.network.forward_order:
            start = data.network.branch_from[branch] - 1
            end = data.network.branch_to[branch] - 1
            voltage[end] = voltage[start] - impedance[branch] * branch_current[branch]
        if np.max(np.abs(voltage - previous)) < 1e-12:
            return voltage
    raise RuntimeError('独立前推回代潮流未收敛。')
