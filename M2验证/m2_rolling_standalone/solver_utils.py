"""统一求解设置和失败处理，模型公式不放在这里。"""
from pathlib import Path
from gurobipy import GRB
import config  # 包含下面要使用的参数


def configure_solver(model, show_log=False, time_limit=120):
    if time_limit is not None and time_limit <= 0:
        raise ValueError('每次求解的时间上限必须为正数。')
    model.Params.OutputFlag = int(show_log)
    model.Params.Threads = config.SOLVER_THREADS                  # 限制使用几个 CPU 核心
    model.Params.Seed = config.SOLVER_SEED                        # 随机种子，为了固定结果
    model.Params.MIPGap = config.SOLVER_MIP_GAP                   # 混合整数规划（MIP）的最优性间隙
    model.Params.MIPGapAbs = config.SOLVER_MIP_GAP_ABS
    model.Params.FeasibilityTol = config.SOLVER_FEASIBILITY_TOL   # 可行性容差
    model.Params.IntFeasTol = config.SOLVER_INTEGER_TOL           # 整数容差
    if time_limit is not None:
        model.Params.TimeLimit = time_limit


def optimize_checked(model, failure_dir=None):
    """仅接受达到指定Gap的最优解；超时/不可行不冒充成功并继续滚动。"""
    model.optimize()
    if model.Status != GRB.OPTIMAL:
        if failure_dir is not None:
            directory = Path(failure_dir)
            directory.mkdir(parents=True, exist_ok=True)
            model.write(str(directory / (model.ModelName + '.lp')))
            if model.Status == GRB.INFEASIBLE:
                model.computeIIS()
                model.write(str(directory / (model.ModelName + '.ilp')))
        raise RuntimeError('模型%s未得到最优解，Gurobi状态码=%s。' % (model.ModelName, model.Status))
    return {'status': 'optimal', 'gurobi_status': int(model.Status),
            'objective': float(model.ObjVal), 'gap': float(model.MIPGap),
            'solver_seconds': float(model.Runtime), 'variables': int(model.NumVars),
            'constraints': int(model.NumConstrs), 'binary_variables': int(model.NumBinVars)}
