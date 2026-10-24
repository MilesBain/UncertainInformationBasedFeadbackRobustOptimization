# 模型能力对比分析

`M1_M2模型能力对比分析报告.pdf` 是9页正式报告，Markdown版本便于修改文字。
报告讨论信息不完备的具体范围、实际电压调控、预测鲁棒性、可解释性、计算和动作代价，以及结论可推广的边界。

本次分析没有改变任何物理参数、优化目标或已执行控制，也没有新增优化求解或AC潮流。
新增的同动作预测诊断与残差分解保存在 `capability_diagnostics.json`，原数据仍在上级 `results`。

## 复核

先用原对比工程的Python环境执行：

```powershell
python analysis/analyze_capability.py
```

该脚本读取两条既有轨迹，在同一轨迹上分别重建静态预测与AC锚定预测，验证误差差分恒等式、原保存预测一致性及越限数量。
同时保存输入文件SHA256、设备动作统计和图表。

报告排版脚本依赖 `reportlab`、`pypdf`、`Pillow` 和Windows微软雅黑字体，本次使用Codex自带的文档Python环境执行：

```powershell
python analysis/build_analysis_report.py
```

此脚本锁定已经审阅的结果版本。重新求解产生新结果后，必须重新核对叙述、数据表和结论；脚本会拦截用新轨迹静默生成旧结论的行为。
`report_validation.json` 保存页数、关键数值及PDF哈希；PDF已逐页渲染检查。

报告中的未来试验明确标记为尚未执行，不能作为当前模型的已验证能力。
