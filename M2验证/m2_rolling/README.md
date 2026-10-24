# M2：真实电压反馈的单步鲁棒滚动优化

本目录实现“代码编写指南/M2模型实现稿.pdf”第6节及“补充要求.md”。
入口为 `run_m2.py`，默认完成M1初始化、23次M2求解、24次真实AC潮流以及独立数值验收。
详细代码阅读说明见 [建模与滚动求解代码详解](docs/建模与滚动求解代码详解.md)，包含脚本角色、函数接口、矩阵与公式、状态时标和调试入口。
原 `code` 文件夹保持不变；没有编写M1/M2性能对比脚本。

## 运行

依赖Python 3.8与有效的Gurobi许可；本机已有的 `rbopt` 环境具备NumPy/SciPy/Gurobi。
本次验证单独创建了继承该环境的虚拟环境并仅安装PYPOWER，没有更改原环境。

在当前机器上可直接运行：

```powershell
& 'D:\StudyAndCompitation\Master\Ucertain\selfResearch\M2验证\m2_rolling\run.ps1'
```

`run.ps1`优先使用本目录`.venv`，其次使用本次已验证的独立环境；可用`-Python`显式指定解释器，
用`-Steps 1 -Output results_single_step`进行单步验证。PowerShell若有脚本执行策略限制，可以直接调用：

```powershell
& 'C:\Users\29909\Documents\Codex\2026-10-22\du-q\work\.venv\Scripts\python.exe' 'D:\StudyAndCompitation\Master\Ucertain\selfResearch\M2验证\m2_rolling\run_m2.py'
```

可移植安装（使用Python 3.8，虚拟环境目录自行指定）：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe run_m2.py
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

单步测试：`python run_m2.py --steps 1 --output results_single_step`。
完整验证：`python run_m2.py --output results`。
可选：`--solver-log` 输出求解日志；`--time-limit 120` 设置每次求解时间限制。
默认额外在5个时间转换上验证对偶式与绝对值式目标值一致，`--skip-absolute-checks` 仅跳过这5次额外求解。
每步的精确盒集、设备、能量、AC验收始终执行。

## 文件职责

| 文件 | 内容 |
|---|---|
| `reference/rb_ieee33_storage.py` | 原M1代码逐字节快照，SHA256校验锁定 |
| `data.py` | 直接读取M1数据，建立A/Bp/Bq/K及固定F/g |
| `controller.py` | 原M1初始化、M2对偶化MILP、独立绝对值形式 |
| `power_flow.py` | PYPOWER runpf真实AC执行、独立径向前推回代验算 |
| `validation.py` | 设备约束、对偶残差、全256顶点、AC物理一致性 |
| `run_m2.py` | 滚动状态管理、CSV/NPZ/JSON导出与验证报告 |
| `run.ps1` | 当前机器的一键运行入口，可指定Python解释器和输出目录 |
| `tests/test_m2.py` | 数学等价性、非零松弛、SOC边界、错误输入等关键测试 |
| `results/验证报告.md` | 本次完整24时段验证结果及适用边界 |

## 与原物理环境一致的部分

所有数据从快照导入，避免人工重录和两套参数漂移。源文件SHA256：
`35b5bd88dce15d12f86d04298175bb7b862c9acb97af4c31552a317c5193ea69`。
测试在原项目存在时还会逐字节比较快照和原文件。

- IEEE33，32条原始支路；基准10 MVA、12.66 kV；根节点电压1 p.u.。
- 原24小时负荷、光伏曲线和各节点P/Q负荷，原4处光伏合计10000 kW。
- 原功率因数0.95、2台SVG、4处电容器的每档容量及最大档数。
- 仅节点18储能：1000 kW充/放电、2000 kWh、±600 kvar、效率0.95、SOC 0.2..0.9、初始0.5。
- 原电压范围0.93..1.05、惩罚权重100与1、步长1小时。
- 原主网P/Q交换范围保留：±20000 kW、±1500 kvar。
- 未知线路仍为第5、12、16、29条，r/x各自±30%，其他线路是零宽度区间。

原M1未定义另一组真实阻抗或AC潮流场景。ACPlant因此使用原BRANCHES数值作为固定theta*，
控制器使用原rho定义的固定盒集U0；不新增随机真实阻抗、不更换标准库中另一版IEEE33数据。
AC模型不增加线路并联电纳、变压器、支路容量或其他原配置没有的设备。
电容器按原文Q=gamma*Q_step执行为恒Q注入，光伏/SVG/ESS同样按优化后的恒P/Q设定执行。
AC负荷母线使用净负荷等效表示设备，所有受控母线仍为PQ母线；只有原根节点为平衡节点。

## 关键实现约定

1. **矩阵排序与符号。** 支路按M1原顺序，非根节点列为物理母线2..33；正号为注入。
   `u=[P_curt,P_ESS,Q_PV,Q_SVG,Q_CB,Q_ESS]`，维度16；弃光在Bp中系数-1。
   功率为p.u.，能量为p.u.·h，AC输出电压幅值平方后传给M2。
2. **单步目标。** 每次只优化下一时段的电压松弛惩罚及弃光，不建立统一24时段M2问题。
   原M1全时域模型只求解一次供初始化使用，其余M1计划均弃置。
3. **鲁棒化。** 全部64个线路r/x分量组成theta，F=[I;-I]、g=[theta_hi;-theta_lo]。
   每个非根节点均施加Fᵀlambda_plus=h、Fᵀlambda_minus=-h及对应非负性/支持函数不等式。
   h=h0+K*(u_next-u_actual)。线路参数不是控制决策变量。
4. **反馈。** 每次执行AC并验证成功后才提交状态；下一步只能读取AC实测电压平方及已执行动作。
   F/g初始化后不修改，不学习参数、不校正预测误差。
5. **储能时间约定。** 使用边界能量 `energy_boundaries[k]` 表示时段k执行前能量。
   时段k动作满足 `E[k+1]=E[k]+eta_ch*Pch[k]*DT-Pdis[k]*DT/eta_dis`。
   时段0动作由M1给出，初始能量为E[0]，执行后E[1]由M1同一条能量方程获得。
   随后优化k=t+1动作时，从E[k]计算E[k+1]。这统一了实现稿(6-17..19)与(6-20)的时标，
   并保证初始动作与随后23次动作都只推进一次能量；不漏计也不重复计量。
6. **主网与线路。** 无根节点控制变量；用非根节点净注入总和约束原P/Q主网交换范围。
   这是保留M1已有的主网范围，未添加支路容量约束。实际AC主网交换含网损，由潮流确定并报告。
   无损耗模型满足交换范围不必然意味着AC交换也满足；报告中单独列出实际超出值。
7. **不增加运行约束。** 没有视在功率耦合、额外动作惩罚、日末SOC回归、预测误差集合或参数在线更新。
   软电压松弛按文稿只要求非负，没有沿用M1的辅助变量数值截断上界；已删除文稿A7排除的支路P/Q人为界限。

## 如何判断可行性

程序不只检查“optimal”。每步检查设备上下界与整数性、充放电互斥、能量递推、主网无损耗交换范围、
H的独立构造、对偶等式/不等式以及精确盒集最坏电压。4条线路的r/x共8个不确定分量还穷举全部256个顶点。
有5次相同输入下的绝对值等价形式求解作为独立数学核验，多解时仅比较目标值与可行性，不要求动作相同。
AC结果用原阻抗重新计算节点功率平衡，并与独立复电流前推回代潮流核对。

数学验收容差为2e-7（对应变量采用p.u.或p.u.·h）。Gurobi可行性与整数容差1e-8；AC功率不平衡容差1e-10。
实际电压越限计数阈值1e-7 p.u.。主网真实P/Q超限及实际电压越限作为运行结果报告，不伪装为求解失败。
对偶MILP求解失败会写出LP，若不可行则输出IIS；AC失败则停止，不把预测值当作反馈继续运行。

**可求解性不等于所有实际AC电压均满足硬限值。** 当前目标没有动作平滑项，零目标值可能对应多组设备动作。
因此不同Gurobi版本或算法设置可能给出不同动作及AC轨迹；固定Threads=1、Seed=0用于本次复现，不能消除数学多解。
报告保留真实越限、预测误差和弃光，不在本阶段改变模型权重或约束以制造“全部达标”。

## 数组索引

`rolling_arrays.npz`中`u/pch/pdis/gamma`和AC电压第一维为24个时段0..23；
`energy_boundaries_pu_h`为25个边界0..24；`voltage_input_sq/control_input_pu/h/lambda_*`为23个转换0->1..22->23。
`voltage_pu`包含根节点33列；`voltage_measured_sq`及预测/松弛数组只含非根节点32列。
控制变量列顺序见`configuration.json/control_order`。NPZ不含pickle对象，可用`numpy.load(..., allow_pickle=False)`读取。

## 方法来源

模型以用户提供的实现稿及补充要求为准。AC调用接口参考
[PYPOWER官方项目](https://github.com/rwl/PYPOWER)及其
[ppoption参数定义](https://github.com/rwl/PYPOWER/blob/master/pypower/ppoption.py)。
