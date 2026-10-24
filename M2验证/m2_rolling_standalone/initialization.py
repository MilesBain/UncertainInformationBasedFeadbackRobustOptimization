"""在本项目内独立建立24时段静态鲁棒模型，只执行第0时段的计划。

这实现文档规定的M1式初始化，不导入M1脚本、不读取其解文件。
从第1时段起，main.py会用M2重新计算动作，舍弃其余静态计划。
"""
from dataclasses import dataclass
from time import perf_counter
import numpy as np
import gurobipy as gp
from device_model import add_devices, curtailment_expression, extract_dispatch
from solver_utils import configure_solver, optimize_checked


@dataclass
class InitialSchedule:
    actions: list
    slack_upper: np.ndarray
    slack_lower: np.ndarray
    predicted_nominal_sq: np.ndarray
    predicted_lower_sq: np.ndarray
    predicted_upper_sq: np.ndarray
    info: dict


def solve_initial_schedule(data, show_log=False, time_limit=120, failure_dir=None):
    """输入独立数据，输出24时段静态计划和求解信息。全过程自行建模。"""
    started = perf_counter()
    with gp.Model('independent_static_initialization') as model:
        configure_solver(model, show_log, time_limit)
        # 保留原静态模型的数值变量界限。这些不是新增线路容量约束。
        branch_p = model.addMVar((data.periods, data.n), lb=-5, ub=5, name='branch_p')
        branch_q = model.addMVar((data.periods, data.n), lb=-5, ub=5, name='branch_q')
        abs_p = model.addMVar((data.periods, data.n), lb=0, ub=5, name='absolute_p')
        abs_q = model.addMVar((data.periods, data.n), lb=0, ub=5, name='absolute_q')
        nominal = model.addMVar((data.periods, data.n), lb=-2, ub=3, name='nominal_voltage_sq')
        half_deviation = model.addMVar((data.periods, data.n), lb=0, ub=2, name='half_deviation')
        upper_slack = model.addMVar((data.periods, data.n), lb=0, ub=1, name='upper_slack')
        lower_slack = model.addMVar((data.periods, data.n), lb=0, ub=1, name='lower_slack')
        variables = []
        objective = data.voltage_weight * (upper_slack.sum() + lower_slack.sum())
        energy_before = data.initial_energy

        for period in range(data.periods):
            devices = add_devices(model, data, period, energy_before)
            variables.append(devices)
            energy_before = devices.energy_after
            objective += data.curtailment_weight * curtailment_expression(data, devices)
            for bus in data.pv_buses:
                reactive = devices.control[data.control_index['qpv', bus]]
                model.addConstr(reactive >= -1)
                model.addConstr(reactive <= 1)

            p = data.p_external[period] + data.Bp @ devices.control
            q = data.q_external[period] + data.Bq @ devices.control
            # 正注入表示发电，正支路潮流表示根节点向下游送电，所以这里有负号。
            model.addConstr(branch_p[period] == -data.A @ p)
            model.addConstr(branch_q[period] == -data.A @ q)
            model.addConstr(abs_p[period] >= branch_p[period])
            model.addConstr(abs_p[period] >= -branch_p[period])
            model.addConstr(abs_q[period] >= branch_q[period])
            model.addConstr(abs_q[period] >= -branch_q[period])
            for node in range(data.n):
                path = data.A[:, node]
                model.addConstr(nominal[period, node] == data.root_voltage ** 2
                                - 2 * (path * data.r_nominal) @ branch_p[period]
                                - 2 * (path * data.x_nominal) @ branch_q[period])
                model.addConstr(half_deviation[period, node] ==
                                (path * data.r_radius) @ abs_p[period]
                                + (path * data.x_radius) @ abs_q[period])
            model.addConstr(nominal[period] - 2 * half_deviation[period]
                            >= data.voltage_min_sq - lower_slack[period])
            model.addConstr(nominal[period] + 2 * half_deviation[period]
                            <= data.voltage_max_sq + upper_slack[period])

        model.setObjective(objective, gp.GRB.MINIMIZE)
        info = optimize_checked(model, failure_dir)
        actions = [extract_dispatch(item) for item in variables]
        # 用实际动作重新计算精确盒式界限，不把可能偏松的辅助绝对值当成真实值。
        nominal_values, lower_values, upper_values = [], [], []
        for period, action in enumerate(actions):
            p, q = data.injections(period, action.control)
            coefficient = data.coefficient_matrix(p, q)
            center = data.root_voltage ** 2 + coefficient @ data.theta_nominal
            radius = np.abs(coefficient) @ data.theta_radius
            nominal_values.append(center)
            lower_values.append(center - radius)
            upper_values.append(center + radius)
        info['total_seconds'] = perf_counter() - started
        info['purpose'] = 'independently built static robust model; execute period 0 only'
        return InitialSchedule(actions, upper_slack.X.copy(), lower_slack.X.copy(),
                               np.array(nominal_values), np.array(lower_values), np.array(upper_values), info)
