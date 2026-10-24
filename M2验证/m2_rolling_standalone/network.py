"""根据本项目的支路表建立径向拓扑，不调用外部网络库的IEEE33模板。"""
from dataclasses import dataclass
import numpy as np


@dataclass
class Network:
    buses: list
    nonroot_buses: list
    root_bus: int
    branch_from: np.ndarray          # 保持物理母线编号1..33
    branch_to: np.ndarray
    parent_branch: dict              # 子母线 -> 上游支路下标(从0开始)
    children_branches: dict          # 母线 -> 下游支路下标列表
    paths: dict                      # 母线 -> 从根节点出发的支路下标列表
    path_matrix: np.ndarray          # 行为支路，列为非根节点
    forward_order: list              # 按深度从根到末端排列的支路下标


def build_network(branches, bus_count, root_bus=1):
    """检查连通径向网络，然后构建路径矩阵。错误拓扑会直接报错。"""
    buses = list(range(1, bus_count + 1))
    if root_bus not in buses or len(branches) != bus_count - 1:
        raise ValueError('根节点或支路数量不符合径向网络要求。')
    parent = {}
    children = {bus: [] for bus in buses}
    for index, (start, end, resistance, reactance) in enumerate(branches):
        if start not in buses or end not in buses or start == end:
            raise ValueError('支路包含非法母线编号或自环。')
        if end == root_bus or end in parent:
            raise ValueError('支路必须朝向下游，每个非根节点只能有一个上游。')
        if resistance <= 0 or reactance <= 0:
            raise ValueError('本模型要求线路电阻、电抗为正。')
        parent[end] = index
        children[start].append(index)

    paths = {root_bus: []}
    nonroot = [bus for bus in buses if bus != root_bus]
    for bus in nonroot:
        current = bus
        reversed_path = []
        visited = set()
        while current != root_bus:
            if current in visited or current not in parent:
                raise ValueError('网络存在环路或节点无法连接到根节点。')
            visited.add(current)
            branch_index = parent[current]
            reversed_path.append(branch_index)
            current = branches[branch_index][0]
        paths[bus] = list(reversed(reversed_path))

    matrix = np.zeros((len(branches), len(nonroot)))
    for column, bus in enumerate(nonroot):
        for branch_index in paths[bus]:
            matrix[branch_index, column] = 1.0
    order = sorted(range(len(branches)), key=lambda index: len(paths[branches[index][1]]))
    return Network(buses, nonroot, root_bus,
                   np.array([b[0] for b in branches]), np.array([b[1] for b in branches]),
                   parent, children, paths, matrix, order)
