"""重点阅读：真实平方电压反馈下的单步M2鲁棒MILP。

五步对应公式：读取状态 -> 添加设备 -> H=H0+K*delta_u -> 鲁棒化 -> 目标与求解。
本函数不会调用AC，也不会修改输入状态；状态推进集中在main.py。
"""
from time import perf_counter
import numpy as np
import gurobipy as gp
from device_model import add_devices, curtailment_expression, extract_dispatch
from solver_utils import configure_solver, optimize_checked
from states import StepResult


def check_state(data, state):
    """在建模前检查状态；避免把电压幅值或错误维度直接混入公式。"""
    if not isinstance(state.period, (int, np.integer)) or not 0 <= state.period < data.periods - 1:
        raise ValueError('反馈时段必须在0至倒数第二时段之间。')
    values = (state.measured_voltage_sq, state.actual_control, state.energy)
    shapes = ((data.n,), (data.control_count,), (len(data.storage_buses),))
    for value, shape in zip(values, shapes):
        if np.shape(value) != shape or not np.isfinite(value).all():
            raise ValueError('输入状态维度错误或含有非有限数值。')
    if np.any(state.measured_voltage_sq <= 0):
        raise ValueError('输入平方电压必须为正。')
    for index, bus in enumerate(data.storage_buses):
        settings = data.storage[bus]
        capacity = settings['e_capacity'] / data.base_kw
        if not settings['soc_min'] * capacity - 1e-8 <= state.energy[index] <= settings['soc_max'] * capacity + 1e-8:
            raise ValueError('反馈储能能量超出物理范围。')


def solve_step(data, state, formulation='dual', show_log=False, time_limit=120, failure_dir=None):
    """由时段t的GridState求解t+1的动作，返回StepResult。

    dual：文档要求的鲁棒对偶式，默认正式控制器。
    absolute：盒式集合的等价绝对值式，仅用于独立核验最优目标。
    """
    check_state(data, state)
    if formulation not in ('dual', 'absolute'):
        raise ValueError('formulation只能为dual或absolute。')
    target_period = state.period + 1
    h_external = data.external_change_matrix(state.period)
    parameter_count = 2 * data.n
    started = perf_counter()
    with gp.Model('M2_t%02d_%s' % (target_period, formulation)) as model:
        configure_solver(model, show_log, time_limit)
        devices = add_devices(model, data, target_period, state.energy)
        # M2松弛量无上界；表示电压软约束，不声称实际AC电压必定合格。
        slack_upper = model.addMVar(data.n, lb=0, name='slack_upper')
        slack_lower = model.addMVar(data.n, lb=0, name='slack_lower')
        delta_control = devices.control - state.actual_control

        dual_upper = None
        dual_lower = None
        if formulation == 'dual':
            dual_upper = model.addMVar((data.n, 2 * parameter_count), lb=0, name='lambda_upper')
            dual_lower = model.addMVar((data.n, 2 * parameter_count), lb=0, name='lambda_lower')
        else:
            absolute_h = model.addMVar((data.n, parameter_count), lb=0, name='absolute_h')

        # 循环构建鲁棒约束
        for node in range(data.n):  
            h = h_external[node] + data.K[node] @ delta_control  # 系统矩阵分解
            measured = state.measured_voltage_sq[node]
            if formulation == 'dual':  # 构建对偶约束
                # F=[I;-I]，因此F.T @ lambda就是前半段减后半段。
                # 使用稀疏的差分写法，数学上仍是文档的F.T lambda = +/-h。
                """model.addConstr(dual_upper[node, :parameter_count]
                                - dual_upper[node, parameter_count:] == h)
                model.addConstr(dual_lower[node, :parameter_count]
                               - dual_lower[node, parameter_count:] == -h)"""
                
                model.addConstr(data.F.T @ dual_upper[node] == h)
                model.addConstr(data.F.T @ dual_lower[node] == -h)
                model.addConstr(data.g @ dual_upper[node]
                                <= data.voltage_max_sq + slack_upper[node] - measured)
                model.addConstr(data.g @ dual_lower[node]
                                <= measured - data.voltage_min_sq + slack_lower[node])
            else:
                model.addConstr(absolute_h[node] >= h)
                model.addConstr(absolute_h[node] >= -h)
                nominal = measured + data.theta_nominal @ h
                radius = data.theta_radius @ absolute_h[node]
                model.addConstr(nominal + radius <= data.voltage_max_sq + slack_upper[node])
                model.addConstr(nominal - radius >= data.voltage_min_sq - slack_lower[node])

        # 目标函数
        objective = data.voltage_weight * (slack_upper.sum() + slack_lower.sum())
        objective += data.curtailment_weight * curtailment_expression(data, devices)
        model.setObjective(objective, gp.GRB.MINIMIZE)
        info = optimize_checked(model, failure_dir)
        
        action = extract_dispatch(devices) # 求解成功后读取数值
        coefficient = np.empty((data.n, parameter_count))
        for node in range(data.n):# 重新算h矩阵（在求解结束后，此时h矩阵为常系数）
            coefficient[node] = h_external[node] + data.K[node] @ (action.control - state.actual_control)

        # 电压递归方程，预测下一时刻电压
        center = state.measured_voltage_sq + coefficient @ data.theta_nominal 
        # 计算由于线路参数不确定，导致的最坏情况电压“浮动半径”
        radius = np.abs(coefficient) @ data.theta_radius

        # 记录求解耗时、电压惩罚、弃光惩罚等信息
        info.update({'period': target_period, 'formulation': formulation,
                     'total_seconds': perf_counter() - started,
                     'voltage_penalty': float(data.voltage_weight * (slack_upper.X.sum() + slack_lower.X.sum())),
                     'curtailment_penalty': float(data.curtailment_weight *
                         sum(action.control[data.control_index['curt', bus]] for bus in data.pv_buses))})

        # 返回所有的计算结果（执行动作、松弛量、预测电压、对偶变量、系统信息）
        return StepResult(action, slack_upper.X.copy(), slack_lower.X.copy(), coefficient,
                          center, center - radius, center + radius,
                          None if dual_upper is None else dual_upper.X.copy(),
                          None if dual_lower is None else dual_lower.X.copy(), info)
