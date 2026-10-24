# -*- coding: utf-8 -*-
"""含储能IEEE33模型的可视化。

只导入 rb_ieee33_storage.py，求解后保存11张PNG和结果摘要。
包括24时段调压、储能充放电、电量/SOC和无功；不导出决策变量或支路潮流图。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np


# 使用非交互绘图后端，并把 Matplotlib 缓存放到项目结果目录。
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_DIR / "results" / "rb_ieee33_storage"
FIGURE_DIR = OUTPUT_DIR / "figures"
MPL_CACHE_DIR = OUTPUT_DIR / ".matplotlib"
for directory in ( FIGURE_DIR, MPL_CACHE_DIR):
    directory.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(MPL_CACHE_DIR))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams

import rb_ieee33_storage as model


rcParams["font.sans-serif"] = [
    "Microsoft YaHei",
    "SimHei",
    "Noto Sans CJK SC",
    "DejaVu Sans",
]
rcParams["axes.unicode_minus"] = False
rcParams["figure.dpi"] = 120
rcParams["savefig.dpi"] = 220
rcParams["font.size"] = 9


POWER_BASE_KW = model.S_BASE * 1000.0


def extract_arrays(x):
    """结果按时段排列在内存中；模型模块同时重算精确最坏电压偏差。"""
    return model.extract_results(x)


def save_figure(fig, filename):
    """统一保存并关闭图形，避免批量绘图时占用过多内存。"""
    path = FIGURE_DIR / filename
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_system_profile(values):
    """绘制全网负荷、可用光伏、实际光伏与弃光。"""
    hours = np.arange(model.T)
    load_p = model.p_load.sum(axis=1) * POWER_BASE_KW
    load_q = model.q_load.sum(axis=1) * POWER_BASE_KW
    pv_available = model.p_av.sum(axis=1) * POWER_BASE_KW
    curtailed = values["curt"].sum(axis=1) * POWER_BASE_KW
    pv_actual = pv_available - curtailed

    fig, ax = plt.subplots(figsize=(9, 4.8))
    ax.plot(hours, load_p, "o-", label="有功负荷", color="#1f77b4")
    ax.plot(hours, load_q, "s-", label="无功负荷", color="#ff7f0e")
    ax.plot(hours, pv_available, "--", label="光伏可用出力", color="#2ca02c")
    ax.plot(hours, pv_actual, "^-", label="光伏实际出力", color="#16823b")
    ax.fill_between(hours, pv_actual, pv_available, color="#d62728", alpha=0.28,
                    label="弃光")
    ax.set(xlabel="时段 (h)", ylabel="功率 (kW / kvar)", title="系统负荷与光伏出力")
    ax.set_xticks(hours)
    ax.grid(alpha=0.25)
    ax.legend(ncol=3)
    return save_figure(fig, "01_system_load_pv.png")


def plot_pv_dispatch(values):
    """分别绘制各光伏节点的可用、实际和弃光功率。"""
    hours = np.arange(model.T)
    pv_buses = sorted(model.PV_BUSES)
    fig, axes = plt.subplots(2, 2, figsize=(10, 7), sharex=True)
    for ax, bus in zip(axes.flat, pv_buses):
        i = bus - 1
        available = model.p_av[:, i] * POWER_BASE_KW
        curtailed = values["curt"][:, i] * POWER_BASE_KW
        actual = available - curtailed
        ax.plot(hours, available, "--", color="#555555", label="可用")
        ax.plot(hours, actual, "o-", color="#2ca02c", label="实际")
        ax.fill_between(hours, actual, available, color="#d62728", alpha=0.3,
                        label="弃光")
        ax.set_title(f"节点 {bus}，装机 {model.PV_BUSES[bus]:.0f} kW")
        ax.grid(alpha=0.25)
        ax.legend()
    for ax in axes[-1]:
        ax.set_xlabel("时段 (h)")
    for ax in axes[:, 0]:
        ax.set_ylabel("功率 (kW)")
    fig.suptitle("各光伏节点出力", fontsize=12)
    return save_figure(fig, "02_pv_dispatch.png")


def plot_reactive_devices(values):
    """绘制光伏逆变器、SVG和电容器的逐时段动作。"""
    hours = np.arange(model.T)
    fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True)

    for bus in sorted(model.PV_BUSES):
        axes[0].plot(hours, values["qpv"][:, bus - 1] * POWER_BASE_KW,
                     marker="o", label=f"节点{bus}")
    axes[0].set_ylabel("Q (kvar)")
    axes[0].set_title("光伏逆变器无功出力")

    for bus in sorted(model.SVC_BUSES):
        axes[1].plot(hours, values["qsvc"][:, bus - 1] * POWER_BASE_KW,
                     marker="s", label=f"节点{bus}")
    axes[1].set_ylabel("Q (kvar)")
    axes[1].set_title("SVG无功出力")

    for bus in sorted(model.CB_BUSES):
        axes[2].step(hours, np.rint(values["gam"][:, bus - 1]), where="mid",
                     label=f"节点{bus}")
    axes[2].set_ylabel("投入档数")
    axes[2].set_xlabel("时段 (h)")
    axes[2].set_title("并联电容器投切")
    axes[2].set_xticks(hours)

    for ax in axes:
        ax.grid(alpha=0.25)
        ax.legend(ncol=4)
    return save_figure(fig, "03_reactive_devices.png")


def plot_grid_exchange(values):
    """绘制主网有功和无功交换，并标出模型限值。"""
    hours = np.arange(model.T)
    pm = values["PM"] * POWER_BASE_KW
    qm = values["QM"] * POWER_BASE_KW
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True)

    axes[0].plot(hours, pm, "o-", color="#1f77b4")
    axes[0].axhline(model.PM_LIM[0], color="#d62728", linestyle="--", label="下限")
    axes[0].axhline(model.PM_LIM[1], color="#d62728", linestyle="--", label="上限")
    axes[0].set_ylabel("PM (kW)")
    axes[0].set_title("主网有功交换")

    axes[1].plot(hours, qm, "s-", color="#ff7f0e")
    axes[1].axhline(model.QM_LIM[0], color="#d62728", linestyle="--", label="下限")
    axes[1].axhline(model.QM_LIM[1], color="#d62728", linestyle="--", label="上限")
    axes[1].set_ylabel("QM (kvar)")
    axes[1].set_xlabel("时段 (h)")
    axes[1].set_title("主网无功交换")
    axes[1].set_xticks(hours)

    for ax in axes:
        ax.grid(alpha=0.25)
        ax.legend()
    return save_figure(fig, "04_grid_exchange.png")


def voltage_envelope(values):
    """由 v0 与 Psi 计算鲁棒电压幅值的最坏下界和上界。"""
    lower_sq = values["v0"] - 2.0 * values["Psi"]
    upper_sq = values["v0"] + 2.0 * values["Psi"]
    lower = np.sqrt(np.clip(lower_sq, 0.0, None))
    upper = np.sqrt(np.clip(upper_sq, 0.0, None))
    return lower, upper


def plot_voltage_heatmaps(values):
    """绘制全部非根节点的鲁棒电压上下界热力图。"""
    lower, upper = voltage_envelope(values)
    lower = lower[:, 1:].T
    upper = upper[:, 1:].T

    fig, axes = plt.subplots(2, 1, figsize=(11, 9), sharex=True)
    color_min = min(model.V_MIN, float(lower.min()))
    color_max = max(model.V_MAX, float(upper.max()))
    for ax, data, title in [
        (axes[0], lower, "最坏电压下界"),
        (axes[1], upper, "最坏电压上界"),
    ]:
        image = ax.imshow(data, aspect="auto", origin="lower", cmap="RdYlGn",
                          vmin=color_min, vmax=color_max,
                          extent=[-0.5, model.T - 0.5, 1.5, model.NB + 0.5])
        ax.set_ylabel("节点编号")
        ax.set_title(title)
        fig.colorbar(image, ax=ax, label="电压 (p.u.)", pad=0.01)
    axes[1].set_xlabel("时段 (h)")
    axes[1].set_xticks(np.arange(model.T))
    return save_figure(fig, "05_voltage_heatmaps.png")


def plot_voltage_profiles(values):
    """绘制全局最高和最低电压所在时段的节点电压包络。"""
    lower, upper = voltage_envelope(values)
    non_root_lower = lower[:, 1:]
    non_root_upper = upper[:, 1:]
    t_low = int(np.unravel_index(np.argmin(non_root_lower), non_root_lower.shape)[0])
    t_high = int(np.unravel_index(np.argmax(non_root_upper), non_root_upper.shape)[0])
    buses = np.arange(2, model.NB + 1)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), sharey=True)
    for ax, t, title in [
        (axes[0], t_low, "最低电压所在时段"),
        (axes[1], t_high, "最高电压所在时段"),
    ]:
        ax.fill_between(buses, model.V_MIN, model.V_MAX, color="#c8e6c9", alpha=0.5,
                        label="允许范围")
        ax.plot(buses, lower[t, 1:], "v-", label="最坏下界", color="#1f77b4")
        ax.plot(buses, upper[t, 1:], "^-", label="最坏上界", color="#d62728")
        ax.axhline(model.V_MIN, color="#333333", linestyle="--")
        ax.axhline(model.V_MAX, color="#333333", linestyle="--")
        ax.set_title(f"{title}：t={t} h")
        ax.set_xlabel("节点编号")
        ax.grid(alpha=0.25)
        ax.legend()
    axes[0].set_ylabel("电压 (p.u.)")
    return save_figure(fig, "06_voltage_profiles.png")


def plot_voltage_regulation_24h(values):
    """展示24时段全网标称电压和鲁棒最坏电压的调压结果。"""
    hours = np.arange(model.T)
    lower, upper = voltage_envelope(values)
    nominal = np.sqrt(np.clip(values["v0"], 0.0, None))

    # 排除根节点，只统计物理节点2～33。
    robust_min = lower[:, 1:].min(axis=1)
    robust_max = upper[:, 1:].max(axis=1)
    nominal_min = nominal[:, 1:].min(axis=1)
    nominal_max = nominal[:, 1:].max(axis=1)

    fig, ax = plt.subplots(figsize=(11, 5.6))
    ax.axhspan(model.V_MIN, model.V_MAX, color="#c8e6c9", alpha=0.38,
               label="允许电压范围")
    ax.fill_between(hours, robust_min, robust_max, color="#90caf9", alpha=0.35,
                    label="鲁棒最坏电压范围")
    ax.plot(hours, robust_min, "v-", color="#1565c0", linewidth=1.8,
            label="各时段最坏电压下界")
    ax.plot(hours, robust_max, "^-", color="#c62828", linewidth=1.8,
            label="各时段最坏电压上界")
    ax.plot(hours, nominal_min, "--", color="#42a5f5", linewidth=1.2,
            label="各时段标称最低电压")
    ax.plot(hours, nominal_max, "--", color="#ef5350", linewidth=1.2,
            label="各时段标称最高电压")
    ax.axhline(model.V_MIN, color="#333333", linestyle=":", linewidth=1)
    ax.axhline(model.V_MAX, color="#333333", linestyle=":", linewidth=1)
    ax.set_xlabel("时段 (h)")
    ax.set_ylabel("电压 (p.u.)")
    ax.set_title("24时段鲁棒调压结果")
    ax.set_xticks(hours)
    ax.grid(alpha=0.25)
    ax.legend(ncol=2)
    return save_figure(fig, "07_voltage_regulation_24h.png")


def plot_voltage_slacks(values):
    """绘制各时段全部节点的电压上下越限松弛量。"""
    hours = np.arange(model.T)
    upper_total = values["sp"][:, 1:].sum(axis=1)
    lower_total = values["sm"][:, 1:].sum(axis=1)
    width = 0.4
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.bar(hours - width / 2, upper_total, width, label="上越限松弛", color="#d62728")
    ax.bar(hours + width / 2, lower_total, width, label="下越限松弛", color="#1f77b4")
    ax.set(xlabel="时段 (h)", ylabel="松弛量合计 (p.u.^2)", title="逐时段电压越限松弛")
    ax.set_xticks(hours)
    ax.grid(alpha=0.25, axis="y")
    ax.legend()
    return save_figure(fig, "08_voltage_slacks.png")


def plot_storage_power(values):
    """各储能节点24时段的充放电：放电正注入，充电负注入。"""
    hours = np.arange(model.T)
    buses = sorted(model.ESS_BUSES)
    fig, axes = plt.subplots(len(buses), 1, figsize=(11, 3.3 * len(buses)),
                             sharex=True, squeeze=False)
    for ax, bus in zip(axes.flat, buses):
        i = bus - 1
        config = model.ESS_BUSES[bus]
        ax.bar(hours, values["pdis"][:, i] * POWER_BASE_KW,
               color="#e28a2b", label="放电（正注入）")
        ax.bar(hours, -values["pch"][:, i] * POWER_BASE_KW,
               color="#2878b5", label="充电（负注入）")
        ax.plot(hours, values["pess"][:, i] * POWER_BASE_KW,
                "o-", color="#333333", markersize=3, label="净有功")
        ax.axhline(config["p_dis_max"], color="#e28a2b", linestyle=":")
        ax.axhline(-config["p_ch_max"], color="#2878b5", linestyle=":")
        ax.axhline(0, color="#777777", linewidth=0.7)
        ax.set(title=f"节点{bus}储能充放电", ylabel="功率 (kW)")
        ax.margins(y=0.25)
        ax.legend(ncol=3, loc="upper left")
        ax.grid(alpha=0.2, axis="y")
    axes[-1, 0].set(xlabel="时段 (h)", xticks=hours)
    return save_figure(fig, "09_storage_charge_discharge.png")


def plot_storage_energy(values):
    """展示25个电量状态；右轴SOC为电量/额定电量。"""
    times = np.arange(model.T + 1)
    buses = sorted(model.ESS_BUSES)
    fig, axes = plt.subplots(len(buses), 1, figsize=(11, 3.5 * len(buses)),
                             sharex=True, squeeze=False)
    for ax, bus in zip(axes.flat, buses):
        config = model.ESS_BUSES[bus]
        capacity = config["e_capacity"]
        ax.axhspan(capacity * config["soc_min"], capacity * config["soc_max"],
                   color="#c8e6c9", alpha=0.5, label="允许电量范围")
        ax.plot(times, values["E"][:, bus - 1] * POWER_BASE_KW,
                "o-", color="#267b57", markersize=4, label="储存电量")
        ax.axhline(capacity * config["soc_initial"], color="#888888",
                   linestyle="--", label="初始电量")
        ax.set(title=f"节点{bus}电量与SOC", ylabel="电量 (kWh)", ylim=(0, capacity))
        right = ax.secondary_yaxis("right", functions=(
            lambda energy, cap=capacity: energy / cap * 100,
            lambda soc, cap=capacity: soc / 100 * cap))
        right.set_ylabel("SOC (%)")
        ax.legend(ncol=3, loc="upper left")
        ax.grid(alpha=0.2)
    axes[-1, 0].set(xlabel="时刻 (h)：0为初始状态，24为日末", xticks=times)
    return save_figure(fig, "10_storage_energy_soc.png")


def plot_storage_reactive(values):
    """储能无功出力按文档采用独立的上下限。"""
    hours = np.arange(model.T)
    buses = sorted(model.ESS_BUSES)
    fig, axes = plt.subplots(len(buses), 1, figsize=(11, 3.0 * len(buses)),
                             sharex=True, squeeze=False)
    for ax, bus in zip(axes.flat, buses):
        qmax = model.ESS_BUSES[bus]["q_max"]
        ax.plot(hours, values["qess"][:, bus - 1] * POWER_BASE_KW,
                "o-", color="#7a5195", markersize=3, label="储能无功")
        ax.axhline(qmax, color="#555555", linestyle="--", label="无功上下限")
        ax.axhline(-qmax, color="#555555", linestyle="--")
        ax.set(title=f"节点{bus}储能无功调节", ylabel="无功 (kvar)")
        ax.margins(y=0.25)
        ax.legend(ncol=2, loc="upper right")
        ax.grid(alpha=0.2)
    axes[-1, 0].set(xlabel="时段 (h)", xticks=hours)
    return save_figure(fig, "11_storage_reactive_power.png")


def export_summary(status, objective, values):
    """保存求解状态和关键指标，便于快速核对结果。"""
    lower, upper = voltage_envelope(values)
    available_kwh = float(model.p_av.sum() * POWER_BASE_KW * model.DT)
    curtailed_kwh = float(values["curt"].sum() * POWER_BASE_KW * model.DT)
    path = OUTPUT_DIR / "summary.txt"
    lines = [
        f"solver={'Gurobi' if model.HAS_GUROBI else 'HiGHS (scipy)'}",
        f"status={status}",
        f"objective={objective:.10f}",
        f"unknown_branches={model.UNKNOWN}",
        f"C_V={model.C_V}; C_CURT={model.C_CURT}",
        f"pv_available_kWh={available_kwh:.4f}",
        f"pv_curtailed_kWh={curtailed_kwh:.4f}",
        f"pv_curtailment_ratio_percent={100.0 * curtailed_kwh / available_kwh:.6f}",
        f"worst_voltage_min_pu={lower[:, 1:].min():.8f}",
        f"worst_voltage_max_pu={upper[:, 1:].max():.8f}",
        f"upper_slack_sum_pu2={values['sp'][:, 1:].sum():.10f}",
        f"lower_slack_sum_pu2={values['sm'][:, 1:].sum():.10f}",
    ]
    for bus, config in model.ESS_BUSES.items():
        i = bus - 1
        lines.extend([
            f"storage_bus{bus}_parameters={config}",
            f"storage_bus{bus}_charged_kWh={values['pch'][:, i].sum() * POWER_BASE_KW * model.DT:.4f}",
            f"storage_bus{bus}_discharged_kWh={values['pdis'][:, i].sum() * POWER_BASE_KW * model.DT:.4f}",
            f"storage_bus{bus}_initial_kWh={values['E'][0, i] * POWER_BASE_KW:.4f}",
            f"storage_bus{bus}_final_kWh={values['E'][-1, i] * POWER_BASE_KW:.4f}",
        ])
    #path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main():
    """求解模型，保存结果摘要并生成调压相关图形。"""
    print("正在求解含储能的鲁棒优化模型……")
    status, objective, x = model.b.solve()
    solver = "Gurobi" if model.HAS_GUROBI else "HiGHS (scipy)"
    print(f"求解器: {solver}；状态: {status}")
    if x is None:
        raise RuntimeError(f"模型没有返回可用解：{status}")

    values = extract_arrays(x)
    summary = export_summary(status, objective, values)

    figures = [
        plot_system_profile(values),
        plot_pv_dispatch(values),
        plot_reactive_devices(values),
        plot_grid_exchange(values),
        plot_voltage_heatmaps(values),
        plot_voltage_profiles(values),
        plot_voltage_regulation_24h(values),
        plot_voltage_slacks(values),
        plot_storage_power(values),
        plot_storage_energy(values),
        plot_storage_reactive(values),
    ]

    print(f"目标值: {objective:.6f}")
    print(f"结果摘要: {summary}")
    print(f"已保存 {len(figures)} 张图片到: {FIGURE_DIR}")


if __name__ == "__main__":
    main()
