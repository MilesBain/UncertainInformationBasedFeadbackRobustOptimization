"""只读取同一次公平对比产生的数值文件，绘制可复核PNG/SVG图。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import numpy as np


def plot_all(out):
    out = Path(out)
    figures = out / 'figures'
    figures.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('MPLCONFIGDIR', str(out / '.matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import rcParams
    rcParams.update({'font.sans-serif': ['Microsoft YaHei', 'SimHei', 'DejaVu Sans'],
                     'axes.unicode_minus': False, 'font.size': 10, 'axes.titlesize': 12,
                     'figure.dpi': 120, 'savefig.dpi': 180,
                     'axes.spines.top': False, 'axes.spines.right': False,
                     'axes.grid': True, 'grid.alpha': .16})
    arrays = {}
    for method in ('M1', 'M2'):
        with np.load(out / (method.lower() + '_results.npz'), allow_pickle=False) as archive:
            arrays[method] = {k: archive[k] for k in archive.files}
    config = json.loads((out / 'shared_scenario.json').read_text(encoding='utf-8'))
    summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
    p = config['original_parameters']
    base, hours = p['S_BASE'] * 1000, np.arange(p['T'])
    vmin, vmax = p['V_MIN'], p['V_MAX']
    colors = {'M1': '#2457A7', 'M2': '#E57924'}
    labels = {'M1': 'M1 静态', 'M2': 'M2 滚动'}
    controls = config['control_order']
    index = {(kind, bus): j for j, (kind, bus) in enumerate(controls)}
    generated = []

    def save(fig, name):
        fig.savefig(figures / (name + '.png'), bbox_inches='tight', facecolor='white')
        fig.savefig(figures / (name + '.svg'), bbox_inches='tight', facecolor='white')
        generated.append(name)
        plt.close(fig)

    def xaxis(ax):
        ax.set_xlim(0, 23)
        ax.set_xticks([0, 4, 8, 12, 16, 20, 23])
        ax.set_xlabel('时段（h，0为共同初始化）')

    fig, axes = plt.subplots(2, 2, figsize=(13, 8.4), layout='constrained')
    for method, a in arrays.items():
        v = a['voltage_pu'][:, 1:]
        ax = axes[0, 0]
        ax.plot(hours, v.max(axis=1), color=colors[method], label=labels[method] + ' 最高')
        ax.plot(hours, v.min(axis=1), '--', color=colors[method], label=labels[method] + ' 最低')
        ax.fill_between(hours, v.min(axis=1), v.max(axis=1), color=colors[method], alpha=.055)
        counts = ((v < vmin - 1e-7) | (v > vmax + 1e-7)).sum(axis=1)
        axes[0, 1].bar(hours + (-.18 if method == 'M1' else .18), counts, width=.34,
                       color=colors[method], label=labels[method])
        axes[1, 0].plot(hours, np.cumsum(a['curtailment_kw']) * p['DT'], '-o', ms=3,
                        color=colors[method], label=labels[method])
        axes[1, 1].plot(hours, a['grid_q_kvar'], '-o', ms=3, color=colors[method], label=labels[method])
    for limit in (vmin, vmax):
        axes[0, 0].axhline(limit, color='#B33D3D', lw=1, ls=':')
    axes[0, 0].set(title='实际 AC 电压范围', ylabel='电压（p.u.）')
    axes[0, 0].legend(ncol=2, fontsize=8)
    axes[0, 1].set(title='逐时段实际电压越限节点数', ylabel='节点数')
    axes[0, 1].yaxis.get_major_locator().set_params(integer=True)
    axes[1, 0].set(title='累计弃光电量', ylabel='累计弃光（kWh）')
    axes[1, 1].set(title='实际主网无功交换（含网损）', ylabel='无功（kvar）')
    for limit in p['QM_LIM']:
        axes[1, 1].axhline(limit, color='#B33D3D', lw=1, ls=':')
    for ax in axes.flat:
        xaxis(ax)
        if ax is not axes[0, 0]:
            ax.legend(fontsize=9)
    fig.suptitle('M1 与 M2：相同物理环境、相同初态、统一 AC 潮流评价', fontsize=15)
    save(fig, '01_actual_performance')

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.7), layout='constrained', sharey=True)
    for ax, (method, a) in zip(axes, arrays.items()):
        mesh = ax.imshow(a['voltage_pu'][:, 1:].T, origin='lower', aspect='auto',
                         extent=(-.5, 23.5, 1.5, 33.5), cmap='RdYlBu_r', vmin=.93, vmax=1.07)
        bad = (a['voltage_pu'][:, 1:] < vmin - 1e-7) | (a['voltage_pu'][:, 1:] > vmax + 1e-7)
        t, j = np.where(bad)
        ax.scatter(t, j + 2, marker='s', s=30, facecolors='none', edgecolors='black', linewidths=.8)
        ax.set(title=labels[method] + '：黑框为实际越限', xlabel='时段（h）', ylabel='物理母线编号')
        ax.grid(False)
        ax.set_xticks([0, 4, 8, 12, 16, 20, 23])
    fig.colorbar(mesh, ax=axes, label='实际 AC 电压（p.u.，共同色标）', shrink=.92)
    fig.suptitle('非根节点电压分布：32节点 × 24时段', fontsize=15)
    save(fig, '02_voltage_heatmaps')

    fig, axes = plt.subplots(2, 1, figsize=(12, 7.5), layout='constrained', sharex=True)
    load = np.array(config['actual_model_arrays']['p_load']).sum(axis=1) * base
    axes[0].plot(hours, load, color='#666666', ls='--', label='共同有功负荷')
    axes[0].plot(hours, arrays['M1']['pv_available_kw'], color='#6B9B69', lw=3, alpha=.6, label='共同可用光伏')
    for method, a in arrays.items():
        axes[0].plot(hours, a['pv_actual_kw'], color=colors[method], label=labels[method] + ' 实际光伏')
        axes[1].bar(hours + (-.18 if method == 'M1' else .18), a['curtailment_kw'], width=.34,
                     color=colors[method], label=labels[method] + ' 弃光')
    axes[0].set(title='相同负荷和光伏下的实际消纳', ylabel='功率（kW）')
    axes[1].set(title='逐时段弃光功率', ylabel='弃光（kW）')
    for ax in axes:
        ax.legend(ncol=2)
        xaxis(ax)
    save(fig, '03_pv_curtailment')

    for bus_str, cfg in p['ESS_BUSES'].items():
        bus = int(bus_str)
        storage_buses = [int(b) for b in p['ESS_BUSES']]
        j = storage_buses.index(bus)
        fig, axes = plt.subplots(3, 1, figsize=(12, 9), layout='constrained')
        for method, a in arrays.items():
            energy = a['energy_boundaries_pu_h'][:, j] * base
            axes[0].plot(np.arange(25), energy / cfg['e_capacity'], '-o', ms=3,
                          color=colors[method], label=labels[method])
            axes[1].step(hours, (a['pdis'][:, j] - a['pch'][:, j]) * base, where='mid',
                          color=colors[method], label=labels[method])
            axes[2].step(hours, a['u'][:, index['qess', bus]] * base, where='mid',
                          color=colors[method], label=labels[method])
        for limit in (cfg['soc_min'], cfg['soc_max']):
            axes[0].axhline(limit, ls=':', color='#B33D3D')
        axes[0].set(title='SOC：共同初始值，未增加日末回归约束', ylabel='SOC', xlabel='能量边界（时段k动作对应k→k+1）', xlim=(0, 24))
        axes[0].set_xticks([0, 4, 8, 12, 16, 20, 24])
        axes[1].set(title='储能净有功：放电为正，充电为负', ylabel='有功（kW）')
        axes[2].set(title='储能无功', ylabel='无功（kvar）')
        for ax in axes[1:]:
            ax.axhline(0, color='#888888', lw=.7)
            xaxis(ax)
        for ax in axes:
            ax.legend()
        fig.suptitle('节点%d储能动作与能量轨迹' % bus, fontsize=15)
        save(fig, '04_storage_bus_%d' % bus)

    fig, axes = plt.subplots(2, 2, figsize=(13, 8), layout='constrained')
    for method, a in arrays.items():
        axes[0, 0].plot(hours, a['grid_p_kw'], color=colors[method], label=labels[method])
        axes[0, 1].plot(hours, a['loss_p_kw'], color=colors[method], label=labels[method])
        axes[1, 0].plot(hours[1:], np.sqrt(np.mean((a['voltage_sq'][1:] - a['nominal_sq'][1:]) ** 2, axis=1)),
                        color=colors[method], label=labels[method])
        penalties = p['C_V'] * (np.maximum(a['voltage_sq'] - p['V_HI_SQ'], 0) +
                                  np.maximum(p['V_LO_SQ'] - a['voltage_sq'], 0)).sum(axis=1)
        cost = penalties + p['C_CURT'] * a['curtailment_kw'] / base
        axes[1, 1].plot(hours, np.cumsum(cost), color=colors[method], label=labels[method])
    axes[0, 0].axhline(0, color='#888888', lw=.7)
    axes[0, 0].set(title='实际主网有功交换：购电为正', ylabel='有功（kW）')
    axes[0, 1].set(title='实际网络有功损耗', ylabel='网损（kW）')
    axes[1, 0].set(title='名义预测误差：共同评价时段1..23', ylabel='电压平方 RMSE（p.u.²）')
    axes[1, 1].set(title='统一实际AC代价累计值（相同权重）', ylabel='实际代价（非优化器目标）')
    for ax in axes.flat:
        xaxis(ax)
        ax.legend()
    save(fig, '05_losses_and_prediction')

    fig, axes = plt.subplots(2, 2, figsize=(13, 8), layout='constrained')
    for ax, kind, title in zip(axes.flat, ('qpv', 'qsvg', 'qcb', 'qess'),
                               ('光伏逆变器', 'SVG', '电容器', '储能')):
        ids = [i for i, (k, b) in enumerate(controls) if k == kind]
        for method, a in arrays.items():
            ax.step(hours, a['u'][:, ids].sum(axis=1) * base, where='mid',
                    color=colors[method], label=labels[method])
        ax.set(title=title + '总无功', ylabel='无功（kvar）')
        ax.legend()
        xaxis(ax)
    fig.suptitle('各类设备无功动作（逐设备数据见CSV）', fontsize=15)
    save(fig, '06_reactive_controls')

    # 以下只画优化器的预测鲁棒结果，两边均排除共同初始化时段0。
    target = hours[1:]
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.4), layout='constrained')
    for method, a in arrays.items():
        lower, upper = a['lower_sq'][1:], a['upper_sq'][1:]
        # 负平方电压无对应实数幅值，显示缺失而不裁剪成0伪装有效电压。
        lower_pu = np.sqrt(np.where(lower >= 0, lower, np.nan))
        upper_pu = np.sqrt(np.where(upper >= 0, upper, np.nan))
        axes[0, 0].plot(target, lower_pu.min(axis=1), '--', color=colors[method], label=labels[method] + ' 最坏下界')
        axes[0, 0].plot(target, upper_pu.max(axis=1), color=colors[method], label=labels[method] + ' 最坏上界')
        axes[0, 1].plot(target, (upper - lower).mean(axis=1), '-o', ms=3,
                       color=colors[method], label=labels[method])
        axes[1, 0].plot(target, (a['slack_plus'][1:] + a['slack_minus'][1:]).sum(axis=1),
                       '-' if method == 'M1' else '--', marker='o' if method == 'M1' else 'x',
                       ms=4, color=colors[method], label=labels[method])
        axes[1, 1].plot(target, np.cumsum(a['model_objective'][1:]), '-o', ms=3,
                       color=colors[method], label=labels[method])
    for limit in (vmin, vmax):
        axes[0, 0].axhline(limit, color='#B33D3D', ls=':', lw=1)
    axes[0, 0].set(title='预测鲁棒电压上下界（全节点极值）', ylabel='预测电压（p.u.）')
    axes[0, 1].set(title='平均逐节点预测区间宽度', ylabel='区间宽度（p.u.²）')
    axes[1, 0].set(title='预测电压上/下限松弛总量', ylabel='松弛（p.u.²）')
    axes[1, 1].set(title='预测鲁棒目标累计值（同一系数）', ylabel='预测目标值')
    if all(np.max(a['slack_plus'][1:] + a['slack_minus'][1:]) < 1e-12 for a in arrays.values()):
        axes[1, 0].text(.5, .75, '两种方法的预测松弛均为 0', transform=axes[1, 0].transAxes,
                        ha='center', va='center', fontsize=12)
    for ax in axes.flat:
        ax.set_xlim(1, 23)
        ax.set_xticks([1, 4, 8, 12, 16, 20, 23])
        ax.set_xlabel('预测目标时段（共同窗口1–23）')
        ax.legend(fontsize=8, ncol=2 if ax is axes[0, 0] else 1)
    fig.suptitle('M1预测鲁棒结果 与 M2预测鲁棒结果', fontsize=15)
    save(fig, '07_robust_prediction_comparison')

    fig, axes = plt.subplots(2, 2, figsize=(13, 8.6), layout='constrained', sharex=True, sharey=True)
    for row, bus in enumerate((18, 33)):
        j = bus - 2
        for col, (method, a) in enumerate(arrays.items()):
            ax = axes[row, col]
            lower = a['lower_sq'][1:, j]
            upper = a['upper_sq'][1:, j]
            lower = np.sqrt(np.where(lower >= 0, lower, np.nan))
            upper = np.sqrt(np.where(upper >= 0, upper, np.nan))
            nominal = a['nominal_sq'][1:, j]
            nominal = np.sqrt(np.where(nominal >= 0, nominal, np.nan))
            ax.fill_between(target, lower, upper, color=colors[method], alpha=.22, label='本方法预测鲁棒区间')
            ax.plot(target, nominal, '--', color=colors[method], label='本方法名义预测')
            ax.plot(target, a['voltage_pu'][1:, bus - 1], '-o', ms=3, color='#222222', label='本方法控制的AC校验')
            for limit in (vmin, vmax):
                ax.axhline(limit, color='#B33D3D', ls=':', lw=1)
            ax.set(title=labels[method] + ' · 母线%d' % bus, ylabel='电压（p.u.）', xlabel='同一目标时段')
            ax.set_xlim(1, 23)
            ax.set_xticks([1, 4, 8, 12, 16, 20, 23])
            ax.legend(fontsize=8)
    fig.suptitle('两种方法各自的预测 与 各自控制结果的AC校验', fontsize=15)
    save(fig, '08_predictions_and_ac')
    return generated


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'results')
    plot_all(parser.parse_args().output)
