"""有意义的回归测试：拓扑、符号、鲁棒化、SOC时序、AC执行、独立性。

运行：python -m unittest discover -s tests -v
需要和主程序相同的依赖与Gurobi许可证；不读取M1或旧M2代码。
"""
import ast
from pathlib import Path
import unittest
import numpy as np
import config
from data import SystemData
from network import build_network
from states import GridState
from initialization import solve_initial_schedule
from m2_model import solve_step, check_state
from ac_power_flow import run_ac
from validation import box_vertices, validate_initial_schedule, validate_step, validate_ac


class DataTests(unittest.TestCase):
    def setUp(self):
        self.data = SystemData()

    def test_radial_paths_accept_unordered_branches(self):
        branches = [(2, 3, 1, 1), (1, 2, 1, 1)]
        network = build_network(branches, 3)
        np.testing.assert_array_equal(network.path_matrix, [[0, 1], [1, 1]])
        self.assertEqual(network.paths[3], [1, 0])
        self.assertEqual(network.forward_order, [1, 0])

    def test_invalid_topology_rejected(self):
        invalid_cases = [
            [(1, 2, 1, 1), (3, 2, 1, 1)],  # 重复父节点
            [(2, 3, 1, 1), (3, 2, 1, 1)],  # 与根节点断开的环
            [(1, 2, 1, 1), (2, 4, 1, 1)],  # 节点越界
            [(1, 2, -1, 1), (2, 3, 1, 1)], # 非法阻抗
        ]
        for branches in invalid_cases:
            with self.subTest(branches=branches), self.assertRaises(ValueError):
                build_network(branches, 3)

    def test_dimensions_and_exact_fixed_environment(self):
        d = self.data
        self.assertEqual(d.A.shape, (32, 32))
        self.assertEqual(d.K.shape, (32, 64, 16))
        self.assertEqual(d.F.shape, (128, 64))
        self.assertEqual(d.p_load.shape, (24, 32))
        self.assertAlmostEqual(d.p_load[18].sum() * d.base_kw, 3715)
        self.assertAlmostEqual(d.q_load[18].sum() * d.base_kw, 2300)
        self.assertAlmostEqual(d.pv_available[13].sum() * d.base_kw, 9700)
        self.assertEqual(d.initial_energy[0] * d.base_kw, 1000)
        self.assertEqual(d.controls[4], ('pess', 18))
        self.assertEqual(d.voltage_weight, 100)
        self.assertEqual(d.curtailment_weight, 1)

    def test_net_injection_sign_and_voltage_drop(self):
        d = self.data
        p, q = np.zeros(d.n), np.zeros(d.n)
        p[d.node_index[18]], q[d.node_index[18]] = -0.01, -0.005
        coefficient = d.coefficient_matrix(p, q)
        # 母线18路径就是支路1～17，负荷会沿这些线路造成压降。
        expected = np.zeros(64)
        expected[:17] = -0.02
        expected[32:49] = -0.01
        np.testing.assert_allclose(coefficient[d.node_index[18]], expected, atol=0, rtol=0)
        self.assertLess(coefficient[d.node_index[18]] @ d.theta_nominal, 0)
        u = np.zeros(d.control_count)
        u[d.control_index['curt', 18]] = 0.01
        u[d.control_index['pess', 18]] = 0.02
        p0, _ = d.injections(12, np.zeros(d.control_count))
        p1, _ = d.injections(12, u)
        self.assertAlmostEqual(p1[d.node_index[18]] - p0[d.node_index[18]], 0.01)

    def test_box_vertices_and_support_function(self):
        d = self.data
        vertices = box_vertices(d)
        self.assertEqual(vertices.shape, (256, 64))
        self.assertLessEqual(np.max(d.F @ vertices.T - d.g[:, None]), 1e-14)
        h = np.linspace(-2, 3, 64)
        upper = h @ d.theta_nominal + np.abs(h) @ d.theta_radius
        lower = h @ d.theta_nominal - np.abs(h) @ d.theta_radius
        self.assertAlmostEqual((vertices @ h).max(), upper, places=12)
        self.assertAlmostEqual((vertices @ h).min(), lower, places=12)

    def test_parameters_readonly_and_changes_detected(self):
        with self.assertRaises(ValueError):
            self.data.F[0, 0] = 2
        before = self.data.fingerprint()
        self.data.svg_limit[18] = 0.05
        self.assertNotEqual(before, self.data.fingerprint())

    def test_runtime_imports_are_local_or_declared(self):
        root = Path(__file__).resolve().parents[1]
        allowed = {'argparse', 'ast', 'copy', 'csv', 'dataclasses', 'datetime', 'hashlib',
                   'importlib', 'itertools', 'json', 'pathlib', 'platform', 'time',
                   'numpy', 'gurobipy', 'pypower'}
        allowed.update(path.stem for path in root.glob('*.py'))
        for path in root.glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name.split('.')[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [(node.module or '').split('.')[0]]
                else:
                    continue
                self.assertTrue(set(modules).issubset(allowed), (path.name, modules))
                if isinstance(node, ast.ImportFrom) and node.module.startswith('importlib'):
                    self.assertEqual(node.module, 'importlib.metadata')


class OptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = SystemData()
        cls.schedule = solve_initial_schedule(cls.data)
        cls.initial_ac = run_ac(cls.data, 0, cls.schedule.actions[0])
        cls.state = GridState(0, cls.initial_ac.voltage_sq,
                              cls.schedule.actions[0].control, cls.schedule.actions[0].energy_after)

    def test_static_schedule_and_reference_optimum(self):
        validate_initial_schedule(self.data, self.schedule)
        # 仅保存一个已核对的验收数值；该数值不会进入任何优化约束或目标。
        self.assertAlmostEqual(self.schedule.info['objective'], 0.3030658284469053, places=7)

    def test_rolling_model_execution_and_energy(self):
        result = solve_step(self.data, self.state)
        validate_step(self.data, self.state, result)
        ac = run_ac(self.data, 1, result.dispatch)
        validate_ac(self.data, ac)
        settings = self.data.storage[18]
        expected = (self.state.energy + settings['eta_ch'] * result.dispatch.charge_power * self.data.dt
                    - result.dispatch.discharge_power * self.data.dt / settings['eta_dis'])
        np.testing.assert_allclose(result.dispatch.energy_after, expected, atol=1e-10)

    def test_dual_absolute_agree_with_positive_voltage_penalty(self):
        # 人工高电压输入仅是数学压力测试，不改变正式物理场景。
        state = GridState(12, np.full(self.data.n, 1.3 ** 2),
                          self.schedule.actions[12].control, self.schedule.actions[12].energy_after)
        dual = solve_step(self.data, state, 'dual')
        absolute = solve_step(self.data, state, 'absolute')
        validate_step(self.data, state, dual)
        validate_step(self.data, state, absolute)
        self.assertGreater(dual.info['objective'], 1.0)
        self.assertAlmostEqual(dual.info['objective'], absolute.info['objective'], places=6)
        # 对照求解可以有不同动作；不能用动作逐元素相等替代数学等价性验证。

    def test_invalid_state_rejected(self):
        invalid_states = [
            GridState(23, self.state.measured_voltage_sq, self.state.actual_control, self.state.energy),
            GridState(0, np.ones(33), self.state.actual_control, self.state.energy),
            GridState(0, np.full(32, np.nan), self.state.actual_control, self.state.energy),
            GridState(0, self.state.measured_voltage_sq, self.state.actual_control, np.array([100.0])),
        ]
        for state in invalid_states:
            with self.subTest(period=state.period), self.assertRaises(ValueError):
                check_state(self.data, state)

    def test_ac_mapping_detects_inconsistent_storage_action(self):
        import copy
        action = copy.deepcopy(self.schedule.actions[0])
        action.control[self.data.control_index['pess', 18]] += 0.01
        with self.assertRaises(RuntimeError):
            run_ac(self.data, 0, action)


class SavedTrajectoryTests(unittest.TestCase):
    def test_feedback_and_energy_continuity_if_verified_run_present(self):
        path = Path(__file__).resolve().parents[1] / 'results' / 'verified_run' / 'trajectory.npz'
        if not path.exists():
            self.skipTest('尚未生成随附的完整验证轨迹；其余测试仍运行。')
        data = SystemData()
        with np.load(path, allow_pickle=False) as saved:
            self.assertEqual(saved['control'].shape, (24, 16))
            np.testing.assert_array_equal(saved['feedback_voltage_sq'], saved['measured_voltage_sq'][:-1])
            np.testing.assert_array_equal(saved['previous_control'], saved['control'][:-1])
            np.testing.assert_array_equal(saved['energy_before'][1:], saved['energy_after'][:-1])
            np.testing.assert_array_equal(saved['energy_before'][0], data.initial_energy)
            settings = data.storage[18]
            expected = (saved['energy_before'] + settings['eta_ch'] * saved['charge_power'] * data.dt
                        - saved['discharge_power'] * data.dt / settings['eta_dis'])
            np.testing.assert_allclose(saved['energy_after'], expected, atol=1e-10)


if __name__ == '__main__':
    unittest.main()
