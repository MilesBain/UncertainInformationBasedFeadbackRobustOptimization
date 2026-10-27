"""读取已保存的 M2 结果并绘图；不导入模型、不求解、不调用 AC 潮流。

命令行：python plot_results.py --results results/某次运行
函数接口：generate_figures(result_dir, buses=None, dpi=200, formats=('png', 'pdf'))
输入只来自该目录的 JSON、CSV、NPZ；输出固定写入该目录的 figure/。
变量名中的 sq 表示平方电压 (p.u.^2)，voltage 表示电压幅值 (p.u.)。
"""
import argparse
import csv
import hashlib
import json
import warnings
from pathlib import Path

import numpy as np


def read_json(path):
    """兼容普通 JSON 和包含顶层“_说明”的 JSON。"""
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def voltage_from_square(square):
    """负的平方电压没有物理幅值，保留为 NaN 缺口，不能静默截成零。"""
    result = np.full(np.shape(square), np.nan, dtype=float)
    np.sqrt(square, out=result, where=np.asarray(square) >= 0)
    return result


def load_results(result_dir):
    """加载一次已完成输出；使用本次参数快照，不读取当前 config.py。"""
    directory = Path(result_dir).expanduser().resolve()
    required = ('parameters.json', 'summary.json', 'periods.csv', 'trajectory.npz')
    for name in required:
        if not (directory / name).is_file():
            raise FileNotFoundError('缺少绘图数据：%s；请指定一次运行的结果目录。' % (directory / name))
    parameters = read_json(directory / 'parameters.json')
    summary = read_json(directory / 'summary.json')
    if 'network' not in parameters or 'attributes' not in parameters:
        raise ValueError('该目录是旧版参数格式，缺少 network/attributes。请选择当前独立项目导出的结果；不从当前配置补造历史参数。')
    with (directory / 'periods.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    # copy 后关闭压缩文件；allow_pickle=False 避免执行来自文件的 Python 对象。
    with np.load(directory / 'trajectory.npz', allow_pickle=False) as archive:
        names = ('voltage_pu', 'measured_voltage_sq', 'predicted_nominal_sq',
                 'predicted_lower_sq', 'predicted_upper_sq', 'slack_upper', 'slack_lower',
                 'control', 'energy_before', 'energy_after')
        trajectory = {key: archive[key].copy() for key in names}
        if 'coefficient' in archive:
            trajectory['coefficient'] = archive['coefficient'].copy()
    periods = np.array([int(row['period']) for row in rows])
    buses = np.array(parameters['network']['nonroot_buses'], dtype=int)
    count, node_count = len(rows), len(buses)
    if not count or not np.array_equal(periods, np.arange(count)):
        raise ValueError('当前输出格式要求时段从 0 开始连续；不能将缺失时段补成零。')
    if summary['executed_periods'] != count:
        raise ValueError('summary.json 与 periods.csv 的执行时段数不一致。')
    for name in names[1:7]:
        if trajectory[name].shape != (count, node_count):
            raise ValueError('%s 的形状应为 (已执行时段数, 非根节点数)。' % name)
    for name, value in trajectory.items():
        if not np.isfinite(value).all():
            raise ValueError('%s 含 NaN/Inf，不能用图掩盖无效数据。' % name)
    attributes = parameters['attributes']
    if trajectory['control'].shape != (count, len(attributes['controls'])):
        raise ValueError('control 的列数与参数快照中的设备映射不一致。')
    if np.any(trajectory['predicted_lower_sq'] > trajectory['predicted_upper_sq'] + 1e-12):
        raise ValueError('预测下界大于上界。')
    return {'directory': directory, 'required': required, 'parameters': parameters,
            'summary': summary, 'rows': rows, 'trajectory': trajectory,
            'periods': periods, 'buses': buses, 'attributes': attributes}


def compute_metrics(data):
    """同一执行时段、同一控制动作、同一权重下，重建预测/AC评价。"""
    values, attr = data['trajectory'], data['attributes']
    actual_sq = values['measured_voltage_sq']
    actual = voltage_from_square(actual_sq)
    predicted = voltage_from_square(values['predicted_nominal_sq'])
    error_sq = actual_sq - values['predicted_nominal_sq']
    error = actual - predicted  # 正值：模型低估了电压；负值：模型高估。
    lower, upper = values['predicted_lower_sq'], values['predicted_upper_sq']
    interval_tolerance = 2e-7  # 与现有 results_io.py 的区间覆盖统计一致，单位 p.u.^2。
    outside = (actual_sq < lower - interval_tolerance) | (actual_sq > upper + interval_tolerance)
    voltage_tolerance = data['summary']['ac_voltage_violation_tolerance_pu']
    low_count = (actual < attr['voltage_min'] - voltage_tolerance).sum(axis=1)
    high_count = (actual > attr['voltage_max'] + voltage_tolerance).sum(axis=1)
    curt_columns = [i for i, (kind, bus) in enumerate(attr['controls']) if kind == 'curt']
    curtailed_pu = values['control'][:, curt_columns].sum(axis=1)
    curtailment_penalty = attr['curtailment_weight'] * curtailed_pu
    predicted_voltage_penalty = attr['voltage_weight'] * (
        values['slack_upper'] + values['slack_lower']).sum(axis=1)
    # AC 评价用平方电压越限量，不能拿幅值越限量代替；目标不另乘 dt。
    actual_voltage_penalty = attr['voltage_weight'] * (
        np.maximum(actual_sq - attr['voltage_max_sq'], 0)
        + np.maximum(attr['voltage_min_sq'] - actual_sq, 0)).sum(axis=1)
    objective = np.array([float(row['objective']) for row in data['rows']])
    return {'actual': actual, 'predicted': predicted, 'error': error, 'error_sq': error_sq,
            'outside': outside, 'width_sq': upper - lower,
            'low_count': low_count, 'high_count': high_count,
            'curtailed_kw': curtailed_pu * attr['base_kw'],
            'curtailment_penalty': curtailment_penalty,
            'predicted_voltage_penalty': predicted_voltage_penalty,
            'actual_voltage_penalty': actual_voltage_penalty,
            'predicted_objective': objective,
            'reconstructed_objective': predicted_voltage_penalty + curtailment_penalty,
            'actual_objective': actual_voltage_penalty + curtailment_penalty,
            'interval_tolerance': interval_tolerance}


def configure_plotting():
    """无界面导出，不弹窗；优先选择 Windows 中可用的中文字体。"""
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import font_manager
    from matplotlib import pyplot as plt
    available = {font.name for font in font_manager.fontManager.ttflist}
    candidates = ['Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC', 'SimSun']
    font = next((name for name in candidates if name in available), 'DejaVu Sans')
    if font == 'DejaVu Sans':
        warnings.warn('未找到中文字体。请安装微软雅黑或 Noto Sans CJK SC 后重绘。')
    plt.rcParams.update({'font.family': 'sans-serif', 'font.sans-serif': [font],
                         'axes.unicode_minus': False, 'font.size': 10,
                         'axes.titlesize': 11, 'axes.labelsize': 10,
                         'legend.fontsize': 9, 'pdf.fonttype': 42})
    return plt, matplotlib.__version__, font


def time_axis(ax, data):
    periods = data['periods']
    ax.set_xlabel('执行时段 t（t=0 为静态初始化，t≥1 为 M2 滚动）')
    ax.set_xticks(np.unique(np.r_[periods[::max(1, len(periods) // 12)], periods[-1]]))
    ax.set_xlim(-0.5, len(periods) - 0.5)


def heatmap(fig, ax, matrix, data, title, unit, **kwargs):
    """输入行是时段、列是节点；图中横轴时段、纵轴物理节点号。"""
    buses = data['buses']
    picture = ax.imshow(matrix.T, aspect='auto', origin='lower', interpolation='nearest', **kwargs)
    indices = np.unique(np.r_[np.arange(0, len(buses), 3), len(buses) - 1])
    ax.set_yticks(indices, [str(buses[i]) for i in indices])
    ax.set_ylabel('非根节点编号')
    ax.set_title(title)
    time_axis(ax, data)
    fig.colorbar(picture, ax=ax, label=unit, shrink=0.9)


def draw_voltage_overview(plt, data, metrics):
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), constrained_layout=True)
    actual, predicted = metrics['actual'], metrics['predicted']
    valid = np.r_[actual.ravel(), predicted[np.isfinite(predicted)]]
    limits = {'vmin': valid.min(), 'vmax': valid.max(), 'cmap': 'viridis'}
    heatmap(fig, axes[0, 0], actual, data, 'AC 执行电压', '电压幅值 / p.u.', **limits)
    heatmap(fig, axes[0, 1], predicted, data, '模型标称预测电压（与左图同色标）', '电压幅值 / p.u.', **limits)
    attr = data['attributes']
    tolerance = data['summary']['ac_voltage_violation_tolerance_pu']
    state = np.zeros_like(actual)
    state[actual < attr['voltage_min'] - tolerance] = -1
    state[actual > attr['voltage_max'] + tolerance] = 1
    from matplotlib.colors import ListedColormap, BoundaryNorm
    palette = ListedColormap(['#2864b7', '#eeeeee', '#d73027'])
    image = axes[1, 0].imshow(state.T, origin='lower', aspect='auto', interpolation='nearest',
                              cmap=palette, norm=BoundaryNorm([-1.5, -0.5, 0.5, 1.5], 3))
    indices = np.arange(0, len(data['buses']), 3)
    axes[1, 0].set_yticks(indices, data['buses'][indices])
    axes[1, 0].set_ylabel('非根节点编号')
    axes[1, 0].set_title('AC 电压越限位置（使用本次保存的统计容差）')
    colorbar = fig.colorbar(image, ax=axes[1, 0], ticks=[-1, 0, 1], shrink=0.9)
    colorbar.ax.set_yticklabels(['欠压', '合格', '过压'])
    time_axis(axes[1, 0], data)
    t = data['periods']
    axes[1, 1].bar(t, metrics['low_count'], label='欠压节点数', color='#2864b7')
    axes[1, 1].bar(t, metrics['high_count'], bottom=metrics['low_count'], label='过压节点数', color='#d73027')
    axes[1, 1].set(title='每时段 AC 越限节点数', ylabel='节点数')
    time_axis(axes[1, 1], data)
    axes[1, 1].legend()
    fig.suptitle('节点电压调控情况 | 已执行 %d 个时段 | 合格范围 %.3f～%.3f p.u.' %
                 (len(t), attr['voltage_min'], attr['voltage_max']))
    return fig


def choose_buses(data, metrics, requested):
    """默认选峰值最高、谷值最低、误差最大和区间漏覆盖最多的节点。"""
    buses = data['buses'].tolist()
    if requested:
        selected = list(dict.fromkeys(requested))
        if any(bus not in buses for bus in selected):
            raise ValueError('--buses 必须是非根节点编号：%s' % buses)
        return selected
    indices = [int(np.argmax(metrics['actual'].max(axis=0))),
               int(np.argmin(metrics['actual'].min(axis=0))),
               int(np.argmax(np.abs(metrics['error_sq']).max(axis=0))),
               int(np.argmax(metrics['outside'].sum(axis=0)))]
    selected = list(dict.fromkeys(buses[i] for i in indices))
    for bus in reversed(buses):
        if len(selected) >= min(4, len(buses)):
            break
        if bus not in selected:
            selected.append(bus)
    return selected


def draw_voltage_traces(plt, data, metrics, selected):
    rows = (len(selected) + 1) // 2
    fig, axes = plt.subplots(rows, 2, figsize=(14, 3.4 * rows + 1), squeeze=False, constrained_layout=True)
    values, attr, t = data['trajectory'], data['attributes'], data['periods']
    lower = voltage_from_square(values['predicted_lower_sq'])
    upper = voltage_from_square(values['predicted_upper_sq'])
    for ax, bus in zip(axes.ravel(), selected):
        node = data['buses'].tolist().index(bus)
        ax.fill_between(t, lower[:, node], upper[:, node], color='#9ecae1', alpha=0.6,
                        label='参数盒集下的预测区间')
        ax.plot(t, metrics['predicted'][:, node], '--', color='#2166ac', label='标称预测')
        ax.plot(t, metrics['actual'][:, node], 'o-', ms=3, color='#b2182b', label='AC 执行')
        ax.axhline(attr['voltage_min'], color='gray', ls=':', label='电压限值')
        ax.axhline(attr['voltage_max'], color='gray', ls=':')
        ax.set(title='节点 %d' % bus, ylabel='电压幅值 / p.u.')
        time_axis(ax, data)
        ax.legend(loc='best', ncol=2, fontsize=8)
        ax.grid(alpha=0.2)
    for ax in axes.ravel()[len(selected):]:
        ax.set_visible(False)
    fig.suptitle('同一时段预测与 AC 执行对比 | 预测区间不是概率置信区间')
    return fig


def draw_errors(plt, data, metrics):
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), constrained_layout=True)
    error, error_sq, t = metrics['error'], metrics['error_sq'], data['periods']
    maximum = max(float(np.max(np.abs(error_sq))), 1e-12)
    heatmap(fig, axes[0, 0], error_sq, data, '有符号误差：AC平方电压 − 标称预测平方电压',
            '误差 / p.u.²', cmap='RdBu_r', vmin=-maximum, vmax=maximum)
    # 所有预测幅值有效时才报告幅值统计；否则保留缺口并在说明中报告无效数。
    axes[0, 1].plot(t, np.mean(np.abs(error), axis=1), 'o-', label='MAE')
    axes[0, 1].plot(t, np.sqrt(np.mean(error ** 2, axis=1)), 's-', label='RMSE')
    axes[0, 1].plot(t, np.max(np.abs(error), axis=1), '.-', label='最大绝对误差')
    axes[0, 1].set(title='每时段幅值预测误差（非根节点）', ylabel='误差 / p.u.')
    axes[0, 1].legend()
    time_axis(axes[0, 1], data)
    cover = (~metrics['outside']).mean(axis=1) * 100
    axes[1, 0].bar(t, cover, color='#3182bd')
    axes[1, 0].set(title='AC 落在鲁棒预测区间内的节点比例', ylabel='节点比例 / %', ylim=(0, 105))
    time_axis(axes[1, 0], data)
    node_rmse = np.sqrt(np.mean(error_sq ** 2, axis=0))
    axes[1, 1].bar(data['buses'], node_rmse, color='#756bb1')
    axes[1, 1].set(title='各节点平方电压 RMSE（全部已执行时段）', xlabel='非根节点编号', ylabel='RMSE / p.u.²')
    fig.suptitle('预测误差与区间覆盖 | 正误差表示模型低估电压 | 覆盖率是本轨迹统计，非概率保证')
    return fig


def draw_objectives(plt, data, metrics):
    fig, axes = plt.subplots(2, 2, figsize=(14, 8), constrained_layout=True)
    t = data['periods']
    predicted, actual = metrics['predicted_objective'], metrics['actual_objective']
    axes[0, 0].plot(t, predicted, 'o--', label='MILP 预测目标')
    axes[0, 0].plot(t, actual, 's-', label='AC 事后评价值')
    axes[0, 0].set_title('逐时段目标：含 t=0 的单时段贡献')
    axes[0, 1].plot(t, np.cumsum(predicted), '--', label='预测累计')
    axes[0, 1].plot(t, np.cumsum(actual), '-', label='AC 累计')
    axes[0, 1].set_title('同一执行范围的累计值')
    axes[1, 0].bar(t - 0.18, metrics['predicted_voltage_penalty'], width=0.36, label='预测电压惩罚')
    axes[1, 0].bar(t + 0.18, metrics['actual_voltage_penalty'], width=0.36, label='AC 电压惩罚')
    axes[1, 0].set_title('电压惩罚：平方电压越限量 × 电压权重')
    axes[1, 1].bar(t, metrics['curtailment_penalty'], color='#31a354', label='两种评价共用的弃光惩罚')
    axes[1, 1].set_title('弃光惩罚：执行弃光功率(p.u.) × 弃光权重')
    for ax in axes.ravel():
        ax.set_ylabel('加权目标值（非 kWh）')
        time_axis(ax, data)
        ax.legend()
        ax.grid(alpha=0.2)
    fig.suptitle('模型预测目标与 AC 事后评价 | 同时段、同权重、同执行控制 | 不使用初始化全日计划总目标')
    return fig


def draw_parameter_set(plt, data):
    """只画快照中真实存在的固定盒集；不能伪造在线集合收缩的历史。"""
    arrays = data['parameters']['arrays']
    center = np.asarray(arrays['theta_nominal'])
    radius = np.asarray(arrays['theta_radius'])
    lower, upper = np.asarray(arrays['theta_lower']), np.asarray(arrays['theta_upper'])
    n = len(center) // 2
    branches = np.arange(1, n + 1)
    fig, axes = plt.subplots(2, 2, figsize=(14, 8), constrained_layout=True)
    for ax, offset, name in [(axes[0, 0], 0, '电阻 r'), (axes[0, 1], n, '电抗 x')]:
        part = slice(offset, offset + n)
        ax.vlines(branches, lower[part], upper[part], color='#3182bd', lw=3, label='允许区间')
        ax.scatter(branches, center[part], color='#d95f02', s=14, label='标称值')
        ax.set(title='%s 的固定区间（所有支路）' % name, xlabel='支路编号（参数快照中的顺序）', ylabel='参数 / p.u.')
        ax.legend()
        ax.grid(alpha=0.2)
    active = np.flatnonzero(radius > 0)
    ax = axes[1, 0]
    if len(active):
        labels = [('r' if i < n else 'x') + str(i % n + 1) for i in active]
        relative = np.divide(radius[active], np.abs(center[active]),
                             out=np.zeros(len(active)), where=center[active] != 0) * 100
        ax.bar(np.arange(len(active)), relative, color='#756bb1')
        ax.set_xticks(np.arange(len(active)), labels, rotation=40)
        ax.set(title='非零不确定维度的相对半径', ylabel='半径 / |标称值| / %', xlabel='参数名 + 支路编号')
    else:
        ax.text(0.5, 0.5, '全部参数半径为 0：集合退化为单点', ha='center', transform=ax.transAxes)
        ax.set_axis_off()
    ax = axes[1, 1]
    ax.text(0.03, 0.93,
            '当前版本使用固定集合 Θ = {θ : Fθ ≤ g}\n\n'
            'parameters.json 只保存一套中心和上下界。\n'
            '本项目没有在线参数学习或集合更新。\n\n'
            '因此没有可绘制的“逐时段集合收缩”记录；\n'
            '下一张图展示的是电压预测区间的变化。\n\n'
            '图中的 r、x 均为标幺值；支路端点见参数快照。',
            va='top', transform=ax.transAxes, fontsize=11, linespacing=1.6)
    ax.set_axis_off()
    fig.suptitle('线路参数不确定集合 | 固定盒集快照 | 不表示估计误差的概率分布')
    return fig


def draw_uncertainty_effect(plt, data, metrics):
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), constrained_layout=True)
    width, t = metrics['width_sq'], data['periods']
    heatmap(fig, axes[0, 0], width, data, '平方电压预测区间全宽：上界 − 下界', '区间宽度 / p.u.²', cmap='YlOrRd')
    axes[0, 1].plot(t, width.mean(axis=1), 'o-', label='节点平均宽度')
    axes[0, 1].plot(t, width.max(axis=1), 's-', label='节点最大宽度')
    axes[0, 1].set(title='固定参数集合产生的时变电压区间', ylabel='区间全宽 / p.u.²')
    axes[0, 1].legend()
    time_axis(axes[0, 1], data)
    values, arrays = data['trajectory'], data['parameters']['arrays']
    actual_sq = values['measured_voltage_sq']
    distance = np.maximum(values['predicted_lower_sq'] - actual_sq, 0) + np.maximum(
        actual_sq - values['predicted_upper_sq'], 0)
    axes[1, 0].plot(t, distance.max(axis=1), 'o-', color='#d73027', label='最大超出量')
    axes[1, 0].plot(t, distance.mean(axis=1), '--', color='#fdae61', label='平均超出量')
    axes[1, 0].set(title='AC 超出预测区间的距离（未扣统计容差）', ylabel='平方电压距离 / p.u.²')
    time_axis(axes[1, 0], data)
    axes[1, 0].legend()
    ax = axes[1, 1]
    radius = np.asarray(arrays['theta_radius'])
    active = np.flatnonzero(radius > 0)
    if 'coefficient' in values and len(active):
        # coefficient[k] 对应执行时段 k+1；不能错位到时段 k。
        coefficient = values['coefficient']
        expected = (len(t) - 1, len(data['buses']), len(radius))
        if coefficient.shape != expected:
            raise ValueError('coefficient 形状与滚动时段不匹配：%s，应为 %s。' % (coefficient.shape, expected))
        contributions = np.mean(np.abs(coefficient[:, :, active]), axis=1) * radius[active]
        n = len(radius) // 2
        for column, parameter in enumerate(active):
            label = ('r' if parameter < n else 'x') + str(parameter % n + 1)
            ax.plot(t[1:], contributions[:, column], '.-', label=label)
        ax.legend(ncol=2, fontsize=8)
        ax.set(title='各不确定参数对区间半径的贡献（节点平均）', ylabel='mean_i(|H[i,j]| × rho[j]) / p.u.²')
        time_axis(ax, data)
    else:
        ax.text(0.5, 0.5, '无滚动 H 系数记录或参数半径全为零\n不补造贡献曲线',
                ha='center', va='center', transform=ax.transAxes)
        ax.set_axis_off()
    fig.suptitle('不确定性的电压影响 | 半径 = |H|ρ | 集合固定，H 随运行状态与控制动作变化')
    return fig


def draw_controls(plt, data, metrics):
    fig, axes = plt.subplots(2, 2, figsize=(14, 8), constrained_layout=True)
    attr, arrays, values, t = data['attributes'], data['parameters']['arrays'], data['trajectory'], data['periods']
    base = attr['base_kw']
    available = np.asarray(arrays['pv_available'])[:len(t)].sum(axis=1) * base
    load = np.asarray(arrays['p_load'])[:len(t)].sum(axis=1) * base
    axes[0, 0].plot(t, available, '--', label='可用光伏')
    axes[0, 0].plot(t, available - metrics['curtailed_kw'], '-', label='执行光伏有功')
    axes[0, 0].plot(t, load, ':', label='有功负荷')
    axes[0, 0].set(title='光伏、弃光与负荷', ylabel='有功功率 / kW')
    for column, (kind, bus) in enumerate(attr['controls']):
        if kind == 'pess':
            axes[0, 1].step(t, values['control'][:, column] * base, where='mid', label='储能节点 %d' % bus)
        elif kind not in ('curt', 'pess'):
            axes[1, 1].step(t, values['control'][:, column] * base, where='mid', label='%s@%d' % (kind, bus))
    axes[0, 1].axhline(0, color='gray', lw=0.7)
    axes[0, 1].set(title='储能执行有功（正：放电；负：充电）', ylabel='有功功率 / kW')
    for column, bus in enumerate(attr['storage_buses']):
        settings = attr['storage'][str(bus)]
        # 拼接执行前 E0 和各时段执行后的 E1...ET，横轴是状态边界，不是时段号。
        energy = np.r_[values['energy_before'][0, column], values['energy_after'][:, column]] * base
        axes[1, 0].plot(np.arange(len(t) + 1), energy, 'o-', ms=3, label='节点 %d 能量' % bus)
        axes[1, 0].axhline(settings['e_capacity'] * settings['soc_min'], color='gray', ls=':')
        axes[1, 0].axhline(settings['e_capacity'] * settings['soc_max'], color='gray', ls=':')
    axes[1, 0].set(title='储能状态与上下限', xlabel='状态边界 k（E[0] 为初值，E[k+1] 为时段 k 执行后）', ylabel='储能能量 / kWh')
    axes[1, 1].set(title='执行无功设定（正值：向电网注入）', ylabel='无功功率 / kvar')
    for ax in (axes[0, 0], axes[0, 1], axes[1, 1]):
        time_axis(ax, data)
    for ax in axes.ravel():
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(ncol=2, fontsize=8)
        ax.grid(alpha=0.2)
    fig.suptitle('实际送入 AC 潮流的设备控制 | 不含另行测量的执行器跟踪误差')
    return fig


# 图名和说明集中定义，生成的 figure/README.md 使用相同顺序。
FIGURE_DESCRIPTIONS = {
    '01_voltage_overview': '全部非根节点的 AC/标称预测电压热图、AC 越限位置和逐时段越限节点数。',
    '02_voltage_prediction_vs_ac': '选定节点的标称预测、鲁棒预测区间和 AC 电压曲线。',
    '03_prediction_errors': '平方电压有符号误差、幅值 MAE/RMSE、区间覆盖率及逐节点误差。',
    '04_objective_comparison': '同一执行时段的 MILP 目标、AC 评价、累计值、电压惩罚与弃光惩罚。',
    '05_parameter_uncertainty_set': '参数快照中的固定线路电阻/电抗盒集及相对半径；没有虚构在线更新。',
    '06_uncertainty_voltage_effect': '电压预测区间宽度变化、AC 超出区间的距离及各参数的滚动半径贡献。',
    '07_executed_controls': '可用/执行光伏、有功负荷、储能有功与能量、设备无功控制。',
}


def safe_number(value):
    """未定义的指标输出 JSON null，不能写非标准 JSON NaN 或补成零。"""
    number = float(value)
    return number if np.isfinite(number) else None


def write_analysis(data, metrics, selected, figure_dir, files, library_version, font):
    """保存图表数值、统计口径和输入文件指纹，便于论文复核。"""
    error, error_sq = metrics['error'], metrics['error_sq']
    summary = data['summary']
    results = {
        'executed_periods': (len(data['periods']), '实际读取的执行时段数；短运行不补齐到 24。'),
        'selected_buses': (selected, '图 02 的物理节点号；默认按极值、误差、区间漏覆盖选择。'),
        'voltage_mae_pu': (safe_number(np.mean(np.abs(error))), '全部已执行时段和非根节点的幅值 MAE，p.u.。'),
        'voltage_rmse_pu': (safe_number(np.sqrt(np.mean(error ** 2))), '全部已执行时段和非根节点的幅值 RMSE，p.u.。'),
        'voltage_sq_rmse': (safe_number(np.sqrt(np.mean(error_sq ** 2))), '平方电压 RMSE，p.u.^2。'),
        'max_abs_error_sq': (float(np.abs(error_sq).max()), '平方电压最大绝对预测误差，p.u.^2。'),
        'interval_coverage_percent': (float((~metrics['outside']).mean() * 100), '本轨迹 AC 区间覆盖率，%；不是概率保证。'),
        'interval_tolerance_sq': (metrics['interval_tolerance'], '区间覆盖统计的绝对容差，p.u.^2，与 results_io.py 一致。'),
        'outside_interval_node_periods': (int(metrics['outside'].sum()), 'AC 超出预测区间的非根节点-时段对数量。'),
        'ac_low_node_periods': (int(metrics['low_count'].sum()), 'AC 欠压节点-时段对数量；使用 summary 中的幅值容差。'),
        'ac_high_node_periods': (int(metrics['high_count'].sum()), 'AC 过压节点-时段对数量；使用 summary 中的幅值容差。'),
        'predicted_executed_objective_sum': (float(metrics['predicted_objective'].sum()), '含 t=0 单时段贡献的预测目标和，不是初始化全日计划目标。'),
        'ac_executed_objective_sum': (float(metrics['actual_objective'].sum()), '同一执行范围的 AC 平方电压越限惩罚 + 执行弃光惩罚之和；不乘 dt。'),
        'ac_objective_summary_difference': (float(metrics['actual_objective'].sum() - summary['ac_realized_objective_same_weights']), '本脚本重建 AC 总评价减 summary 保存值；用于核对文件一致性。'),
        'predicted_objective_reconstruction_max_error': (float(np.abs(metrics['predicted_objective'] - metrics['reconstructed_objective']).max()), '从保存的松弛量和动作重建目标，与 periods.csv objective 的最大绝对差。'),
        'negative_predicted_square_entries': (int(sum((data['trajectory'][key] < 0).sum() for key in ('predicted_nominal_sq', 'predicted_lower_sq', 'predicted_upper_sq'))), '不能开平方的预测中心/边界元素总数；幅值图保留缺口，指标未定义时为 null。'),
        'parameter_fingerprint_unchanged': (summary.get('parameter_fingerprint_before') == summary.get('parameter_fingerprint_after'), '原输出保存的前后参数指纹是否一致；不是逐时段参数学习记录。'),
        'input_sha256': ({name: hashlib.sha256((data['directory'] / name).read_bytes()).hexdigest() for name in data['required']}, '绘图所读取的四个文件的 SHA256，用于追溯数据版本。'),
        'matplotlib_version': (library_version, '本次绘图使用的 Matplotlib 版本。'),
        'font': (font, '本次绘图选用的字体。'),
    }
    annotated = {'_说明': {key: explanation for key, (value, explanation) in results.items()}}
    annotated.update({key: value for key, (value, explanation) in results.items()})
    (figure_dir / 'plot_metrics.json').write_text(json.dumps(annotated, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    columns = {
        'period': ('执行时段编号，0 为初始化执行。', data['periods']),
        'predicted_objective': ('该时段 MILP 预测目标，来自 periods.csv。', metrics['predicted_objective']),
        'ac_objective': ('同权重 AC 事后评价；平方电压越限惩罚 + 执行弃光惩罚。', metrics['actual_objective']),
        'predicted_voltage_penalty': ('预测松弛量总和乘电压权重。', metrics['predicted_voltage_penalty']),
        'ac_voltage_penalty': ('实际平方电压越限总量乘电压权重。', metrics['actual_voltage_penalty']),
        'curtailment_penalty': ('执行弃光功率(p.u.)乘弃光权重；两种评价共用。', metrics['curtailment_penalty']),
        'voltage_mae_pu': ('该时段所有非根节点的幅值 MAE，p.u.；空白表示未定义。', np.mean(np.abs(error), axis=1)),
        'voltage_rmse_pu': ('该时段所有非根节点的幅值 RMSE，p.u.；空白表示未定义。', np.sqrt(np.mean(error ** 2, axis=1))),
        'voltage_sq_rmse': ('该时段平方电压 RMSE，p.u.^2。', np.sqrt(np.mean(error_sq ** 2, axis=1))),
        'interval_coverage_percent': ('该时段 AC 在预测区间内的节点比例，%。', (~metrics['outside']).mean(axis=1) * 100),
        'mean_interval_width_sq': ('该时段预测区间全宽的节点平均值，p.u.^2。', metrics['width_sq'].mean(axis=1)),
        'ac_low_nodes': ('该时段实际欠压节点数，使用保存的幅值容差。', metrics['low_count']),
        'ac_high_nodes': ('该时段实际过压节点数，使用保存的幅值容差。', metrics['high_count']),
    }
    with (figure_dir / 'plot_period_metrics.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in zip(*(value for description, value in columns.values())):
            writer.writerow([safe_number(value) for value in row])
    csv_notes = '# plot_period_metrics.csv 字段说明\n\n| 字段 | 含义 |\n|---|---|\n'
    csv_notes += ''.join('| `%s` | %s |\n' % (key, value[0]) for key, value in columns.items())
    (figure_dir / 'plot_period_metrics.csv.说明.md').write_text(csv_notes, encoding='utf-8')
    notes = '''# 本次结果可视化

本目录只分析上级目录已经保存的数据，不重新优化，不执行 AC 潮流，不读取当前 config.py。
实际读取 **%d 个时段（0～%d）**，所有电压对比均针对非根节点；根节点由电源固定电压。
图 02 节点：%s。默认优先选实际最高/最低电压、最大平方预测误差、区间漏覆盖最多的节点，去重后补足四个。

## 图表

%s

每张图提供选定格式；默认 PNG 用于查看，PDF 用于矢量排版。重复运行覆盖本脚本同名输出，不改变原始数据。

## 必须区分的统计口径

- t=0 是静态初始化动作的预测/执行；t≥1 是 M2 单步预测/执行。全程指标包含两部分。
- 电压图用 p.u.；模型、鲁棒区间和目标重建用平方电压 p.u.^2。负的预测平方值不会截成零。
- 有符号预测误差 = AC − 标称预测，正值表示模型低估电压。
- 参数集合是固定的线路参数盒集，没有在线收缩历史；变化的是 H 和其映射出的电压区间。
- 预测区间是该线性模型在参数集合上的最坏情形范围，不是概率置信区间，也不是 AC 安全保证。
- 预测目标 = 电压权重 × 预测松弛量之和 + 弃光权重 × 执行弃光功率(p.u.)。
- AC 评价 = 电压权重 × AC 平方电压越限量之和 + 弃光权重 × 同一执行弃光功率(p.u.)。
- 两个目标均按相同执行时段计算，未额外乘时间步长；电量才按功率乘 dt 换算。
- 不将初始化全日计划目标当成时段0目标，也不将未执行的初始化计划当成 M1 的 AC 执行结果。
- 控制图展示送入 AC 的设定，未记录独立执行器跟踪测量，不能据此评价执行器误差。
- 幅值越限统计使用 summary 中的容差；区间覆盖使用 2e-7 p.u.^2；AC 目标用未扣容差的越限量。

## 数值复核

- 相同执行范围的预测目标和：%.12g。
- 相同执行范围的 AC 评价和：%.12g。
- 重建 AC 评价与 summary 保存值之差：%.3g。
- AC 区间覆盖率：%.6f%%（本次单条轨迹）。
- 逐时段表：`plot_period_metrics.csv`；字段说明：`plot_period_metrics.csv.说明.md`。
- 汇总和输入指纹：`plot_metrics.json`，其 `_说明` 解释每个字段。
- `plot_status.json` 的 completed=true 表示本次图表已全部生成；失败时可能保留上一次同名图，应先检查状态。
''' % (len(data['periods']), len(data['periods']) - 1, selected,
       '\n'.join('- `%s`：%s' % item for item in FIGURE_DESCRIPTIONS.items()),
       metrics['predicted_objective'].sum(), metrics['actual_objective'].sum(),
       results['ac_objective_summary_difference'][0], results['interval_coverage_percent'][0])
    (figure_dir / 'README.md').write_text(notes, encoding='utf-8')


def generate_figures(result_dir, buses=None, dpi=200, formats=('png',)):
    """唯一对外绘图接口，返回 figure 目录；可由 results_io 或命令行调用。

    result_dir: 已保存的一次运行目录，不是项目目录或 results 总目录。
    buses: 图02要画的非根节点号，None 时自动选择；不限制其他热图节点。
    dpi/formats: 图片分辨率和格式，默认 200 dpi PNG + 矢量 PDF。
    """
    if not isinstance(dpi, int) or dpi < 72:
        raise ValueError('dpi 应为不小于 72 的整数。')
    formats = tuple(dict.fromkeys(formats))
    if not formats or any(item not in ('png', 'pdf', 'svg') for item in formats):
        raise ValueError('formats 只支持 png、pdf、svg。')
    data = load_results(result_dir)
    metrics = compute_metrics(data)
    selected = choose_buses(data, metrics, buses)
    figure_dir = data['directory'] / 'figure'
    figure_dir.mkdir(exist_ok=True)
    status_path = figure_dir / 'plot_status.json'
    status = {'_说明': {'completed': '本次绘图是否完整成功；不是优化是否成功。',
                       'files': '本次成功生成的图文件名。', 'error': '失败原因；成功时为 null。'},
              'completed': False, 'files': [], 'error': None}
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    plt = None
    try:
        plt, library_version, font = configure_plotting()
        jobs = [lambda: draw_voltage_overview(plt, data, metrics),
                lambda: draw_voltage_traces(plt, data, metrics, selected),
                lambda: draw_errors(plt, data, metrics),
                lambda: draw_objectives(plt, data, metrics),
                lambda: draw_parameter_set(plt, data),
                lambda: draw_uncertainty_effect(plt, data, metrics),
                lambda: draw_controls(plt, data, metrics)]
        for name, draw in zip(FIGURE_DESCRIPTIONS, jobs):
            figure = draw()
            try:
                for extension in formats:
                    filename = '%s.%s' % (name, extension)
                    figure.savefig(figure_dir / filename, dpi=dpi, bbox_inches='tight', facecolor='white')
                    status['files'].append(filename)
            finally:
                plt.close(figure)
        write_analysis(data, metrics, selected, figure_dir, status['files'], library_version, font)
        status['completed'] = True
        previous_error = figure_dir / 'plotting_error.txt'
        if previous_error.exists():
            previous_error.unlink()
    except Exception as error:
        status['error'] = '%s: %s' % (type(error).__name__, error)
        raise
    finally:
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    return figure_dir


def main():
    parser = argparse.ArgumentParser(description='读取一次 M2 结果，输出到该结果目录下的 figure/；不运行优化或 AC。')
    parser.add_argument('--results', required=True, type=Path, help='一次运行的结果目录，内含 trajectory.npz 等')
    parser.add_argument('--buses', nargs='+', type=int, help='图02要画的非根节点号，如 --buses 6 18 25 33')
    parser.add_argument('--dpi', type=int, default=200, help='PNG 分辨率，默认 200')
    parser.add_argument('--formats', nargs='+', choices=['png', 'pdf', 'svg'], default=['png'])
    args = parser.parse_args()
    directory = generate_figures(args.results, args.buses, args.dpi, args.formats)
    print('可视化完成：%s' % directory)


if __name__ == '__main__':
    main()
