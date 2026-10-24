"""数据容器：把函数接口需要传递的内容写清楚，不在容器内求解。"""
from dataclasses import dataclass
import numpy as np


@dataclass
class Dispatch:
    control: np.ndarray              # 16个设备净功率设定，p.u.
    charge_power: np.ndarray         # 每台储能的充电功率，非负
    discharge_power: np.ndarray      # 每台储能的放电功率，非负
    charge_on: np.ndarray            # 充电许可0/1
    discharge_on: np.ndarray         # 放电许可0/1
    capacitor_steps: np.ndarray      # 电容器整数档位
    energy_after: np.ndarray         # 本时段执行后能量，p.u.·h


@dataclass
class StepResult:
    dispatch: Dispatch                 # 控制指令等，详见上面
    slack_upper: np.ndarray
    slack_lower: np.ndarray
    coefficient: np.ndarray            # 系统矩阵（行向量形式）
    predicted_nominal_sq: np.ndarray   # 标称情况下的电压预测
    predicted_lower_sq: np.ndarray     # 鲁棒下边界
    predicted_upper_sq: np.ndarray     # 鲁棒上边界
    dual_upper: object                 # 正对偶变量
    dual_lower: object                 # 负对偶变量
    info: dict                         # 求解日志


@dataclass
class GridState:
    period: int                     # 最近成功执行的时段
    measured_voltage_sq: np.ndarray # 该时段的AC电压平方，非根节点
    actual_control: np.ndarray      # 已执行动作
    energy: np.ndarray              # 下一时段执行前能量


@dataclass
class PowerFlowResult:
    voltage_complex: np.ndarray     # 含根节点33个复电压
    voltage_pu: np.ndarray          # 含根节点33个电压幅值
    voltage_sq: np.ndarray          # 非根节点32个电压平方
    p_injected: np.ndarray
    q_injected: np.ndarray
    grid_p: float                   # p.u.
    grid_q: float
    loss_p: float
    loss_q: float
    branch_p_from: np.ndarray
    branch_q_from: np.ndarray
    branch_p_to: np.ndarray
    branch_q_to: np.ndarray
    mapping_error: float
    balance_error: float
    runtime_seconds: float
