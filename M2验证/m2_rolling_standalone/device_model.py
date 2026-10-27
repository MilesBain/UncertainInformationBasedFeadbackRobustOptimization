"""设备的共同物理约束。初始化和M2都调用这里，避免两套参数产生偏差。"""
from dataclasses import dataclass
import numpy as np
from gurobipy import GRB
from states import Dispatch


@dataclass
class DeviceVariables:
    control: object
    charge: object
    discharge: object
    charge_on: object
    discharge_on: object
    capacitor_steps: object
    energy_after: object


def add_devices(model, data, period, energy_before):
    """给已有Gurobi模型添加一个时段的设备约束，返回变量，不执行求解。

    energy_before可以是数值数组（M2），也可以是前一时段变量（初始化）。
    所有功率均为p.u.；能量为p.u.·h；period从0开始。
    """
    prefix = 't%02d_' % period
    u = model.addMVar(data.control_count, lb=-GRB.INFINITY, name=prefix + 'control')
    number_of_storage = len(data.storage_buses)
    charge = model.addMVar(number_of_storage, lb=0, name=prefix + 'charge')
    discharge = model.addMVar(number_of_storage, lb=0, name=prefix + 'discharge')
    charge_on = model.addMVar(number_of_storage, vtype=GRB.BINARY, name=prefix + 'charge_on')
    discharge_on = model.addMVar(number_of_storage, vtype=GRB.BINARY, name=prefix + 'discharge_on')
    energy_after = model.addMVar(number_of_storage, lb=0, name=prefix + 'energy_after')
    steps = model.addMVar(len(data.capacitor_buses), lb=0, vtype=GRB.INTEGER,
                         name=prefix + 'capacitor_steps')

    for bus in data.pv_buses:
        curtailed = u[data.control_index['curt', bus]]
        reactive = u[data.control_index['qpv', bus]]
        available = data.pv_available[period, data.node_index[bus]]
        model.addConstr(curtailed >= 0)
        model.addConstr(curtailed <= available)
        # 无功能力取决于实际光伏有功；夜间光伏不能提供无功。
        model.addConstr(reactive <= data.pv_factor * (available - curtailed))
        model.addConstr(reactive >= -data.pv_factor * (available - curtailed))

    for bus in data.svg_buses:
        reactive = u[data.control_index['qsvg', bus]]
        model.addConstr(reactive >= -data.svg_limit[bus])
        model.addConstr(reactive <= data.svg_limit[bus])

    for index, bus in enumerate(data.capacitor_buses):
        step_size, maximum_step = data.capacitors[bus]
        model.addConstr(steps[index] <= maximum_step)
        model.addConstr(u[data.control_index['qcb', bus]] == step_size * steps[index])

    for index, bus in enumerate(data.storage_buses):
        settings = data.storage[bus]
        capacity = settings['e_capacity'] / data.base_kw
        model.addConstr(charge[index] <= settings['p_ch_max'] / data.base_kw * charge_on[index])
        model.addConstr(discharge[index] <= settings['p_dis_max'] / data.base_kw * discharge_on[index])
        model.addConstr(charge_on[index] + discharge_on[index] <= 1)
        model.addConstr(u[data.control_index['pess', bus]] == discharge[index] - charge[index])
        model.addConstr(u[data.control_index['qess', bus]] >= -settings['q_max'] / data.base_kw)
        model.addConstr(u[data.control_index['qess', bus]] <= settings['q_max'] / data.base_kw)
        model.addConstr(energy_after[index] >= settings['soc_min'] * capacity)
        model.addConstr(energy_after[index] <= settings['soc_max'] * capacity)
        model.addConstr(energy_after[index] == energy_before[index]
                        + settings['eta_ch'] * charge[index] * data.dt
                        - discharge[index] * data.dt / settings['eta_dis'])

    # 优化中的主网功率按无损功率平衡定义。
    # AC潮流会另行计算包含损耗的主网功率，两个数不应强制相等。
    grid_p = -data.p_external[period].sum() - data.Bp.sum(axis=0) @ u
    grid_q = -data.q_external[period].sum() - data.Bq.sum(axis=0) @ u
    model.addConstr(grid_p >= data.grid_p_limits[0])
    model.addConstr(grid_p <= data.grid_p_limits[1])
    model.addConstr(grid_q >= data.grid_q_limits[0])
    model.addConstr(grid_q <= data.grid_q_limits[1])
    return DeviceVariables(u, charge, discharge, charge_on, discharge_on, steps, energy_after)


def curtailment_expression(data, variables):
    """目标函数中的总弃光功率，保持p.u.单位，不额外乘10000或时长。"""
    indices = [data.control_index['curt', bus] for bus in data.pv_buses]
    return variables.control[indices].sum()


def extract_dispatch(variables):
    """求解成功后读取数值；不取整、不裁剪，保证AC执行的是原求解动作。"""
    return Dispatch(np.array(variables.control.X), np.array(variables.charge.X),
                    np.array(variables.discharge.X), np.array(variables.charge_on.X),
                    np.array(variables.discharge_on.X), np.array(variables.capacitor_steps.X),
                    np.array(variables.energy_after.X))
