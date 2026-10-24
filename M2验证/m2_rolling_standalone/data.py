"""把config中的工程参数转换为优化所需的标幺数组和矩阵。事实建模从这个脚本开始。
"""
import copy
import hashlib
import json
import numpy as np
import config
from network import build_network


def as_json(value):
    """把NumPy值转成可写JSON的数据；字典键统一转字符串。"""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): as_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [as_json(item) for item in value]
    return value


class SystemData:
    """一个实例代表一套固定环境；时序数组按[时段, 非根节点]排列。"""

    def __init__(self):
        self.network = build_network(config.BRANCHES, config.BUS_COUNT, config.ROOT_BUS)
        self.node_index = {bus: index for index, bus in enumerate(self.network.nonroot_buses)}
        self.n = len(self.node_index)
        self.periods = config.PERIOD_COUNT
        self.dt = config.TIME_STEP_HOURS
        self.base_mva = config.BASE_POWER_MVA
        self.base_kv = config.BASE_VOLTAGE_KV
        self.base_kw = self.base_mva * 1000.0
        self.base_ohm = self.base_kv ** 2 / self.base_mva
        self.root_voltage = config.ROOT_VOLTAGE_PU
        self.voltage_min = config.MIN_VOLTAGE_PU
        self.voltage_max = config.MAX_VOLTAGE_PU
        self.voltage_min_sq = self.voltage_min ** 2
        self.voltage_max_sq = self.voltage_max ** 2
        self.voltage_weight = config.VOLTAGE_PENALTY
        self.curtailment_weight = config.CURTAILMENT_PENALTY
        self.pv_factor = float(np.tan(np.arccos(config.PV_MIN_POWER_FACTOR)))
        self.grid_p_limits = np.array(config.GRID_P_LIMIT_KW) / self.base_kw
        self.grid_q_limits = np.array(config.GRID_Q_LIMIT_KVAR) / self.base_kw
        self.storage = copy.deepcopy(config.STORAGE)
        self.pv_buses = list(config.PV_CAPACITY_KW)
        self.svg_buses = list(config.SVG_CAPACITY_KVAR)
        self.capacitor_buses = list(config.CAPACITORS)
        self.storage_buses = list(self.storage)
        self.svg_limit = {bus: value / self.base_kw for bus, value in config.SVG_CAPACITY_KVAR.items()}
        self.capacitors = {bus: (step / self.base_kw, count) for bus, (step, count) in config.CAPACITORS.items()}
        if len(config.LOAD_PROFILE) != self.periods or len(config.PV_PROFILE) != self.periods:
            raise ValueError('负荷、光伏曲线长度必须等于时段数。')
        for mapping in (config.LOADS, config.PV_CAPACITY_KW, config.SVG_CAPACITY_KVAR,
                        config.CAPACITORS, config.STORAGE):
            if any(bus not in self.node_index for bus in mapping):
                raise ValueError('设备和负荷必须连接到已定义的非根节点。')

        self.p_load = np.zeros((self.periods, self.n))
        self.q_load = np.zeros((self.periods, self.n))
        self.pv_available = np.zeros((self.periods, self.n))
        for bus, (p_kw, q_kvar) in config.LOADS.items():
            column = self.node_index[bus]
            # 分步除法与既有参数的标幺定义一致，避免不必要的浮点舍入差异。
            self.p_load[:, column] = p_kw / 1000.0 / self.base_mva * np.array(config.LOAD_PROFILE)
            self.q_load[:, column] = q_kvar / 1000.0 / self.base_mva * np.array(config.LOAD_PROFILE)
        for bus, capacity in config.PV_CAPACITY_KW.items():
            self.pv_available[:, self.node_index[bus]] = np.array(config.PV_PROFILE) * (capacity / 1000.0 / self.base_mva)
        self.p_external = self.pv_available - self.p_load
        self.q_external = -self.q_load

        # 控制顺序明确写出：弃光、储能有功、光伏/SVG/电容/储能无功。
        self.controls = []
        for kind, buses in [('curt', self.pv_buses), ('pess', self.storage_buses),
                            ('qpv', self.pv_buses), ('qsvg', self.svg_buses),
                            ('qcb', self.capacitor_buses), ('qess', self.storage_buses)]:
            for bus in buses:
                self.controls.append((kind, bus))
        self.control_index = {item: index for index, item in enumerate(self.controls)}
        self.control_count = len(self.controls)
        self.A = self.network.path_matrix
        self.Bp = np.zeros((self.n, self.control_count))
        self.Bq = np.zeros_like(self.Bp)
        for column, (kind, bus) in enumerate(self.controls):
            row = self.node_index[bus]
            if kind == 'curt':
                self.Bp[row, column] = -1.0   # 弃光减少注入
            elif kind == 'pess':
                self.Bp[row, column] = 1.0   # 储能放电增加注入
            else:
                self.Bq[row, column] = 1.0

        self.r_nominal = np.array([b[2] for b in config.BRANCHES]) / self.base_ohm
        self.x_nominal = np.array([b[3] for b in config.BRANCHES]) / self.base_ohm
        self.r_radius = np.zeros(self.n)
        self.x_radius = np.zeros(self.n)
        for branch_number, fraction in config.UNCERTAIN_BRANCHES.items():
            if not 1 <= branch_number <= self.n or not 0 <= fraction < 1:
                raise ValueError('不确定支路编号或相对半径不合法。')
            index = branch_number - 1
            self.r_radius[index] = fraction * self.r_nominal[index]
            self.x_radius[index] = fraction * self.x_nominal[index]
        self.theta_nominal = np.concatenate([self.r_nominal, self.x_nominal])
        self.theta_radius = np.concatenate([self.r_radius, self.x_radius])
        self.theta_lower = self.theta_nominal - self.theta_radius
        self.theta_upper = self.theta_nominal + self.theta_radius
        self.F = np.vstack([np.eye(2 * self.n), -np.eye(2 * self.n)])
        self.g = np.concatenate([self.theta_upper, -self.theta_lower])

        # K[i]的形状为(64,16)：动作变化 -> 节点i的线路参数系数变化。    
        # 控制灵敏度矩阵
        self.K = np.zeros((self.n, 2 * self.n, self.control_count))
        branch_p_control = self.A @ self.Bp
        branch_q_control = self.A @ self.Bq
        for node in range(self.n):
            for branch in range(self.n):
                for control in range(self.control_count):
                    self.K[node, branch, control] = 2 * self.A[branch, node] * branch_p_control[branch, control]
                    self.K[node, self.n + branch, control] = 2 * self.A[branch, node] * branch_q_control[branch, control]
        self.initial_energy = np.array([self.storage[b]['soc_initial'] * self.storage[b]['e_capacity'] / self.base_kw
                                        for b in self.storage_buses])
        for array in vars(self).values():
            if isinstance(array, np.ndarray):
                array.setflags(write=False)

    def injections(self, period, control):
        """返回非根节点净注入，正号表示发电/向网络注入。"""
        return (self.p_external[period] + self.Bp @ control,
                self.q_external[period] + self.Bq @ control)

    def external_change_matrix(self, current_period):
        """只计算负荷/光伏变化产生的H0，不含设备动作变化。"""
        if not 0 <= current_period < self.periods - 1:
            raise ValueError('当前时段必须有一个可用的下一时段。')
        delta_p = self.p_external[current_period + 1] - self.p_external[current_period]
        delta_q = self.q_external[current_period + 1] - self.q_external[current_period]
        return self.coefficient_matrix(delta_p, delta_q)

    def coefficient_matrix(self, p, q):
        """H乘theta得到LinDistFlow电压变化，H的形状为(32,64)。"""
        branch_p = self.A @ p
        branch_q = self.A @ q
        result = np.zeros((self.n, 2 * self.n))
        for node in range(self.n):
            for branch in range(self.n):
                result[node, branch] = 2 * self.A[branch, node] * branch_p[branch]
                result[node, self.n + branch] = 2 * self.A[branch, node] * branch_q[branch]
        return result

    def snapshot(self):
        """保存全部显式配置和实际使用的标幺数组，便于独立核对。"""
        physical = {key: value for key, value in vars(config).items() if key.isupper()}
        arrays = {key: value for key, value in vars(self).items() if isinstance(value, np.ndarray)}
        attributes = {key: value for key, value in vars(self).items()
                      if key != 'network' and not isinstance(value, np.ndarray)}
        return as_json({'config': physical, 'arrays': arrays, 'attributes': attributes,
                        'network': vars(self.network)})

    def fingerprint(self):
        text = json.dumps(self.snapshot(), sort_keys=True, ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
