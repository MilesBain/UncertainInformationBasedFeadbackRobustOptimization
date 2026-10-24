# -*- coding: utf-8 -*-
"""
极简 IEEE 33 节点拓扑图：
  - 节点编号 0..32（根=0，对应原母线 1..33）
  - 支路编号 0..31（与 BRANCHES 列表顺序一致）
  - 无设备、无负荷、无颜色分类
"""

import numpy as np
import matplotlib.pyplot as plt
from matplotlib import rcParams

rcParams["font.sans-serif"] = [
    "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
    "WenQuanYi Zen Hei", "SimHei", "DejaVu Sans",
]
rcParams["axes.unicode_minus"] = False
rcParams["figure.dpi"] = 90
rcParams["font.size"] = 9


# ==========================================================================
# 1. 数据：支路 (from, to)，母线编号 1..33
# ==========================================================================
BRANCHES = [
    (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 9),
    (9, 10), (10, 11), (11, 12), (12, 13), (13, 14), (14, 15), (15, 16),
    (16, 17), (17, 18), (2, 19), (19, 20), (20, 21), (21, 22), (3, 23),
    (23, 24), (24, 25), (6, 26), (26, 27), (27, 28), (28, 29), (29, 30),
    (30, 31), (31, 32), (32, 33),
]

NB = 33
NBR = len(BRANCHES)


# ==========================================================================
# 2. 竖版布局：根在上，深度向下，分支横向展开
# ==========================================================================
def radial_layout_vertical():
    children = [[] for _ in range(NB)]
    for u, v in BRANCHES:
        children[u - 1].append(v - 1)

    pos = {}
    leaf = [0]

    def dfs(node, depth):
        ch = children[node]
        if not ch:
            pos[node] = (leaf[0], depth)
            leaf[0] += 1
            return pos[node][0]
        xs_ = [dfs(c, depth + 1) for c in ch]
        x = float(np.mean(xs_))
        pos[node] = (x, depth)
        return x

    dfs(0, 0)

    xs = np.array([pos[i][0] for i in range(NB)])
    ys = np.array([pos[i][1] for i in range(NB)])

    for d in np.unique(ys):
        idx = np.where(ys == d)[0]
        order = np.argsort(xs[idx])
        xs[idx[order]] = np.arange(len(idx))
    return xs, ys


# ==========================================================================
# 3. 画图
# ==========================================================================
def plot_topology(save=None):
    xs, ys = radial_layout_vertical()

    fig, ax = plt.subplots(figsize=(7, 11))

    # ---- 支路 ----
    for k, (u, v) in enumerate(BRANCHES):
        i, j = u - 1, v - 1
        ax.plot([xs[i], xs[j]], [ys[i], ys[j]],
                "-", color="#333333", lw=1.2, zorder=1)

    # ---- 支路编号：放在中点，白底小框 ----
    for k, (u, v) in enumerate(BRANCHES):
        i, j = u - 1, v - 1
        xm = (xs[i] + xs[j]) / 2
        ym = (ys[i] + ys[j]) / 2
        ax.text(xm, ym, str(k),
                fontsize=7, color="#d62728",
                ha="center", va="center", zorder=3,
                bbox=dict(boxstyle="circle,pad=0.15",
                          fc="white", ec="#d62728", lw=0.5))

    # ---- 节点 ----
    ax.scatter(xs, ys, s=50, c="#1f77b4",
               edgecolors="k", linewidths=0.7, zorder=4)

    # ---- 节点编号 0..32：放节点右侧 ----
    for i in range(NB):
        ax.text(xs[i] + 0.18, ys[i], str(i),
                fontsize=8.5, ha="left", va="center",
                color="#1f77b4", fontweight="bold", zorder=5)

    ax.set_title("IEEE 33 节点配电网拓扑\n"
                 "蓝=节点编号 (0–32)   红=支路编号 (0–31)")
    ax.invert_yaxis()
    ax.set_xticks([])
    ax.set_yticks([])
    ax.margins(x=0.12, y=0.04)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout()
    if save:
        fig.savefig(save, bbox_inches="tight")
    return fig


if __name__ == "__main__":
    plot_topology(save="ieee33_topology_ids.png")
    print("已输出 ieee33_topology_ids.png")
    plt.show()