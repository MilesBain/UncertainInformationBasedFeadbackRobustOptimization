"""项目入口；重点阅读run_simulation，看清AC反馈和能量状态如何逐步推进。"""
import argparse
from datetime import datetime
from pathlib import Path
import numpy as np
from data import SystemData
from initialization import solve_initial_schedule
from m2_model import solve_step
from ac_power_flow import run_ac
from states import GridState
from validation import validate_initial_schedule, validate_dispatch, validate_step, validate_ac, assert_checks
from results_io import save_results, write_json


def run_simulation(output_dir, rolling_steps=23, verify_all=False, show_log=False, time_limit=120):
    """执行初始化+rolling_steps次M2；成功返回摘要字典，失败抛出异常。

    正常完整日为24个时段：第0时段初始化，随后滚动求解1～23时段。
    每个已执行时段都核验设备、SOC、鲁棒性和AC；verify_all控制额外等价模型复算。
    """
    data = SystemData()
    if not isinstance(rolling_steps, int) or not 0 <= rolling_steps < data.periods:
        raise ValueError('rolling_steps必须为0至%d的整数。' % (data.periods - 1))
    directory = Path(output_dir).resolve()
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError('输出目录已有内容，请使用新的--output目录：%s' % directory)
    directory.mkdir(parents=True, exist_ok=True)
    fingerprint = data.fingerprint()
    failure_dir = directory / 'failure_diagnostics'
    records = []

    # 一、用本项目自己的数据和代码构造静态初始化，执行的只有第0个动作。
    print('建立并求解独立24时段静态初始化模型……', flush=True)
    schedule = solve_initial_schedule(data, show_log, time_limit, failure_dir)
    checks = validate_initial_schedule(data, schedule)
    initial_action = schedule.actions[0]
    checks.update(validate_dispatch(data, 0, initial_action, data.initial_energy))
    initial_ac = run_ac(data, 0, initial_action)
    checks.update(validate_ac(data, initial_ac))
    initial_objective = data.voltage_weight * (schedule.slack_upper[0].sum() + schedule.slack_lower[0].sum())
    initial_objective += data.curtailment_weight * sum(initial_action.control[data.control_index['curt', b]] for b in data.pv_buses)
    initial_info = dict(schedule.info, objective=float(initial_objective), period=0)
    records.append({'period': 0, 'source': 'static_initialization', 'action': initial_action,
                    'ac': initial_ac, 'energy_before': data.initial_energy.copy(),
                    'nominal_sq': schedule.predicted_nominal_sq[0], 'lower_sq': schedule.predicted_lower_sq[0],
                    'upper_sq': schedule.predicted_upper_sq[0], 'slack_upper': schedule.slack_upper[0],
                    'slack_lower': schedule.slack_lower[0], 'info': initial_info, 'checks': checks})
    # E0 --执行时段0--> E1；第一次M2的energy输入必须是E1，不能重复使用E0。
    state = GridState(0, initial_ac.voltage_sq.copy(), initial_action.control.copy(), initial_action.energy_after.copy())
    print('t=00 初始化执行成功，AC电压 %.6f～%.6f p.u.' %
          (initial_ac.voltage_pu.min(), initial_ac.voltage_pu.max()), flush=True)

    # 二、每次只规划下一个时段；下一次必须读这次执行得到的AC电压。
    for target in range(1, rolling_steps + 1):
        result = solve_step(data, state, 'dual', show_log, time_limit, failure_dir)
        checks = validate_step(data, state, result)
        if verify_all or target in (1, 6, 12, 18, 23):
            alternative = solve_step(data, state, 'absolute', show_log, time_limit, failure_dir)
            validate_step(data, state, alternative)
            difference = abs(result.info['objective'] - alternative.info['objective'])
            checks.update(assert_checks({'dual_absolute_objective': difference}))
            # 等价模型可能有不同最优动作；仅正式对偶模型的动作进入AC执行。
        ac_result = run_ac(data, target, result.dispatch)
        checks.update(validate_ac(data, ac_result))
        records.append({'period': target, 'source': 'M2_dual', 'action': result.dispatch, 'ac': ac_result,
                        'state_before': state, 'energy_before': state.energy.copy(), 'step': result,
                        'nominal_sq': result.predicted_nominal_sq, 'lower_sq': result.predicted_lower_sq,
                        'upper_sq': result.predicted_upper_sq, 'slack_upper': result.slack_upper,
                        'slack_lower': result.slack_lower, 'info': result.info, 'checks': checks})
        # 只有求解、设备检查和AC全部成功，才提交下一状态。
        state = GridState(target, ac_result.voltage_sq.copy(), result.dispatch.control.copy(),
                          result.dispatch.energy_after.copy())
        if data.fingerprint() != fingerprint:
            raise AssertionError('物理环境或固定不确定集合在滚动过程中被改变。')
        print('t=%02d 目标=%.8g，AC电压 %.6f～%.6f p.u.，储能=%s kWh' %
              (target, result.info['objective'], ac_result.voltage_pu.min(), ac_result.voltage_pu.max(),
               np.round(state.energy * data.base_kw, 4).tolist()), flush=True)

    # 三、统一保存真实轨迹、预测结果和验证证据。
    options = {'rolling_steps': rolling_steps, 'verify_all': verify_all, 'show_log': show_log, 'time_limit': time_limit}
    summary = save_results(directory, data, schedule, records, fingerprint, options)
    print('完成，结果已保存：%s' % directory, flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description='独立M2：静态初始化 + 单步鲁棒MILP + AC反馈')
    parser.add_argument('--output', type=Path, help='新的输出目录；默认写入项目results下带时间戳的子目录')
    parser.add_argument('--rolling-steps', type=int, default=23, help='滚动次数0～23，默认完整23次')
    parser.add_argument('--verify-all', action='store_true', help='每一步都额外求解绝对值等价模型核验')
    parser.add_argument('--solver-log', action='store_true', help='显示Gurobi详细日志')
    parser.add_argument('--time-limit', type=float, default=120, help='单个MILP最长秒数，超时即停止')
    args = parser.parse_args()
    output = args.output or Path(__file__).parent / 'results' / datetime.now().strftime('run_%Y%m%d_%H%M%S_%f')
    had_existing_content = output.exists() and (not output.is_dir() or any(output.iterdir()))
    try:
        run_simulation(output, args.rolling_steps, args.verify_all, args.solver_log, args.time_limit)
    except Exception as error:
        # 不覆盖已有成功结果；本次新目录中的失败信息保留，异常仍以非零状态退出。
        if not had_existing_content and output.is_dir() and not (output / 'summary.json').exists():
            write_json(output / 'failure.json', {'completed': False, 'error': str(error), 'type': type(error).__name__})
        raise


if __name__ == '__main__':
    main()
