# -*- coding: utf-8 -*-
"""
IEEE 33 节点拓扑图（严格正交 / 标签零遮挡 / 更大更粗 / 编号从 1 开始 / 图例左下角）
  - 节点编号 1..33，支路编号 1..32
  - 内部坐标仍用 0-based，仅在显示时 +1
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib import rcParams

rcParams["font.sans-serif"] = [
    "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
    "WenQuanYi Zen Hei", "SimHei", "DejaVu Sans",
]
rcParams["axes.unicode_minus"] = False
rcParams["font.size"] = 13


# ==========================================================================
# 1. 数据
# ==========================================================================
BRANCHES = [
    (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 9),
    (9, 10), (10, 11), (11, 12), (12, 13), (13, 14), (14, 15), (15, 16),
    (16, 17), (17, 18), (2, 19), (19, 20), (20, 21), (21, 22), (3, 23),
    (23, 24), (24, 25), (6, 26), (26, 27), (27, 28), (28, 29), (29, 30),
    (30, 31), (31, 32), (32, 33),
]

UNCERTAIN_BRANCHES_0 = {4: 0.30, 11: 0.30, 15: 0.30, 28: 0.30}

PV_CAPACITY_KW    = {18: 3000.0, 22: 2000.0, 25: 2000.0, 33: 3000.0}
SVG_CAPACITY_KVAR = {18: 600.0, 33: 600.0}
CAPACITORS        = {6: (150.0, 4), 12: (150.0, 4), 24: (150.0, 4), 30: (150.0, 4)}
STORAGE = {18: {"e_capacity": 2000.0}}

NB = 33


# ==========================================================================
# 2. 严格正交网格（0-based）
# ==========================================================================
POS = {
    0:(0, 0),    1:(0, -1.5),  2:(0, -3),    3:(0, -4.5),
    4:(0, -6),   5:(0, -7.5),
    6:(2, -7.5), 7:(2, -6),    8:(2, -4.5),  9:(2, -3),
    10:(2, -1.5),11:(2, 0),
    12:(4, 0),   13:(4, -1.5), 14:(4, -3),   15:(4, -4.5),
    16:(4, -6),  17:(4, -7.5),
    18:(-2, -1.5), 19:(-2, 0), 20:(-2, 1.5), 21:(-2, 3),
    22:(-2, -3), 23:(-2, -4.5), 24:(-2, -6),
    25:(0, -9), 26:(2, -9), 27:(2, -10.5), 28:(0, -10.5),
    29:(0, -12), 30:(-2, -12), 31:(-2, -10.5), 32:(-2, -9),
}


# ==========================================================================
# 3. 画图
# ==========================================================================
def plot_topology(save=None):
    fig, ax = plt.subplots(figsize=(18, 18))

    for k, (u1, v1) in enumerate(BRANCHES):
        u, v = u1 - 1, v1 - 1
        x1, y1 = POS[u]; x2, y2 = POS[v]
        assert x1 == x2 or y1 == y2, f"支路{k+1}({u+1}->{v+1}) 不是横平竖直！"

    # ---- 支路 ----
    for k, (u1, v1) in enumerate(BRANCHES):
        u, v = u1 - 1, v1 - 1
        x1, y1 = POS[u]; x2, y2 = POS[v]
        unc = k in UNCERTAIN_BRANCHES_0
        ax.plot([x1, x2], [y1, y2],
                color="#d62728" if unc else "#333333",
                lw=7.0 if unc else 4.2,
                ls="--" if unc else "-",
                solid_capstyle="butt", zorder=1)

    # ---- 支路编号 & ±30%（显示 k+1） ----
    for k, (u1, v1) in enumerate(BRANCHES):
        u, v = u1 - 1, v1 - 1
        x1, y1 = POS[u]; x2, y2 = POS[v]
        xm, ym = (x1 + x2) / 2, (y1 + y2) / 2
        if y1 == y2:
            num_xy = (xm, ym + 0.76); num_ha, num_va = "center", "bottom"
            pct_xy = (xm, ym - 0.76); pct_ha, pct_va = "center", "top"
        else:
            num_xy = (xm + 0.68, ym); num_ha, num_va = "left", "center"
            pct_xy = (xm - 0.68, ym); pct_ha, pct_va = "right", "center"

        unc = k in UNCERTAIN_BRANCHES_0
        if unc:
            ax.text(num_xy[0], num_xy[1], str(k + 1),
                    fontsize=17, color="white", fontweight="bold",
                    ha=num_ha, va=num_va, zorder=3,
                    bbox=dict(boxstyle="circle,pad=0.22",
                              fc="#d62728", ec="#d62728", lw=1.0))
            ax.text(pct_xy[0], pct_xy[1],
                    f"±{UNCERTAIN_BRANCHES_0[k]*100:.0f}%",
                    fontsize=14, color="#d62728", fontweight="bold",
                    ha=pct_ha, va=pct_va, zorder=6,
                    bbox=dict(boxstyle="round,pad=0.16",
                              fc="mistyrose", ec="#d62728", lw=0.9, alpha=0.95))
        else:
            ax.text(num_xy[0], num_xy[1], str(k + 1),
                    fontsize=16, color="#d62728",
                    ha=num_ha, va=num_va, zorder=3,
                    bbox=dict(boxstyle="circle,pad=0.18",
                              fc="white", ec="#d62728", lw=0.9))

    # ---- 节点 ----
    xs = [POS[i][0] for i in range(NB)]
    ys = [POS[i][1] for i in range(NB)]
    ax.scatter(xs, ys, s=420, c="#1f77b4",
               edgecolors="k", linewidths=1.5, zorder=4)

    # ---- 节点编号（显示 i+1） ----
    for i in range(NB):
        ax.text(POS[i][0] + 0.26, POS[i][1] + 0.24, str(i + 1),
                fontsize=18, ha="left", va="bottom",
                color="#1f77b4", fontweight="bold", zorder=5)

    # ---- 设备标签 ----
    label_cfg = {
        6:  (-4.6, -7.5, "right",  "center"),
        12: ( 2.0,  2.2, "center", "bottom"),
        18: ( 6.6, -7.5, "left",   "center"),
        22: (-4.6,  3.0, "right",  "center"),
        24: (-4.6, -4.5, "right",  "center"),
        25: (-4.6, -6.0, "right",  "center"),
        30: ( 0.0, -14.6, "center", "top"),
        33: (-4.6, -9.0, "right",  "center"),
    }

    device_nodes = sorted(set(list(PV_CAPACITY_KW) + list(SVG_CAPACITY_KVAR) +
                              list(CAPACITORS) + list(STORAGE)))
    for n1 in device_nodes:
        n0 = n1 - 1
        lines = []
        if n1 in PV_CAPACITY_KW:
            lines.append(f"PV {PV_CAPACITY_KW[n1]/1000:.1f} MW")
        if n1 in STORAGE:
            lines.append(f"ESS {STORAGE[n1]['e_capacity']/1000:.1f} MWh")
        if n1 in SVG_CAPACITY_KVAR:
            lines.append(f"SVG {SVG_CAPACITY_KVAR[n1]:.0f} kvar")
        if n1 in CAPACITORS:
            q, cnt = CAPACITORS[n1]
            lines.append(f"CB {q*cnt:.0f} kvar")
        text = f"Bus {n1}\n" + "\n".join(lines)

        lx, ly, ha_, va_ = label_cfg[n1]
        ax.annotate(
            text,
            xy=(POS[n0][0], POS[n0][1]),
            xytext=(lx, ly),
            fontsize=17.0, ha=ha_, va=va_, color="darkgreen",
            bbox=dict(boxstyle="round,pad=0.34",
                      fc="honeydew", ec="darkgreen", lw=1.4, alpha=0.97),
            arrowprops=dict(
                arrowstyle="-|>", color="darkgreen", lw=1.8,
                shrinkA=6, shrinkB=14,
                connectionstyle="arc3,rad=0",
            ),
            zorder=7,
        )

    # ---- 标题 / 图例（左下角） ----
    ax.set_title("IEEE 33 节点配电网拓扑（严格正交 / 标签零遮挡）\n"
                 "蓝=节点编号 (1–33)   红圈=支路编号 (1–32)   "
                 "红虚线/红底编号=不确定参数支路 (±30%)   绿框=设备挂接",
                 fontsize=24, fontweight="bold")

    legend_items = [
        mpatches.Patch(color="#1f77b4", label="节点 (1–33)"),
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="white",
                   markeredgecolor="#d62728", markersize=18,
                   label="确定参数支路编号"),
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="#d62728",
                   markeredgecolor="#d62728", markersize=18,
                   label="不确定参数支路编号"),
        plt.Line2D([0], [0], color="#d62728", lw=6.0, ls="--",
                   label="不确定参数支路 (±30%)"),
        mpatches.Patch(facecolor="honeydew", edgecolor="darkgreen",
                       label="设备挂接（外围引线）"),
    ]
    # ★ 图例移到左下角
    ax.legend(handles=legend_items, loc="lower left",
              bbox_to_anchor=(1, 0),
              fontsize=18, framealpha=0.97,
              borderpad=0.9, labelspacing=0.7,
              handlelength=1.8, handleheight=1.3)

    ax.set_aspect("auto")
    ax.set_xticks([]); ax.set_yticks([])
    ax.margins(x=0.26, y=0.10)
    for sp in ax.spines.values():
        sp.set_visible(False)
    fig.tight_layout()
    if save:
        fig.savefig(save, bbox_inches="tight")
    return fig


if __name__ == "__main__":
    plot_topology(save="ieee33_topology_ids.png")
    plt.close("all")
    print("已输出 ieee33_topology_ids.png")