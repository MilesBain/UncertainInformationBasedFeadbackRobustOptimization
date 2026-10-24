"""关键数学/时序/物理回归测试；不进行M1与M2性能对比。"""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data import ModelData, load_legacy, SOURCE, SOURCE_SHA256
from controller import initialize_m1, solve_step, Dispatch
from power_flow import ACPlant, backward_forward_check
from validation import verify_step, verify_devices, verify_power_flow, box_vertices


class M2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = ModelData(load_legacy())
        cls.action, _ = initialize_m1(cls.data)
        cls.plant = ACPlant(cls.data.legacy)
        cls.flow = cls.plant.run(cls.data, 0, cls.action)

    def test_01_original_source_is_unchanged(self):
        self.assertEqual(hashlib.sha256(SOURCE.read_bytes()).hexdigest(), SOURCE_SHA256)
        original = SOURCE.parents[2] / 'code' / SOURCE.name
        if original.exists():
            self.assertEqual(SOURCE.read_bytes(), original.read_bytes())

    def test_02_physical_data_and_dimensions(self):
        d, old = self.data, self.data.legacy
        self.assertEqual((d.n, d.nu, d.periods), (32, 16, 24))
        np.testing.assert_array_equal(d.p_available, old.p_av[:, 1:])
        np.testing.assert_array_equal(d.p_load, old.p_load[:, 1:])
        np.testing.assert_array_equal(d.q_load, old.q_load[:, 1:])
        np.testing.assert_array_equal(self.plant.r_true, np.array(old.BRANCHES)[:, 2] / old.Z_BASE)
        np.testing.assert_array_equal(self.plant.x_true, np.array(old.BRANCHES)[:, 3] / old.Z_BASE)
        self.assertEqual(set(old.UNKNOWN), {5, 12, 16, 29})
        self.assertEqual(box_vertices(d).shape, (256, 64))
        self.assertEqual(d.ess, (18,))
        self.assertTrue(self.plant.check_box_membership(d))

    def test_03_topology_kcl_independently(self):
        d, old = self.data, self.data.legacy
        rng = np.random.RandomState(90)
        injection = rng.normal(0, .03, 32)
        branch = -d.A @ injection
        for node in range(1, old.NB):
            balance = sum(branch[k] for k in old.children_br[node]) - injection[node - 1]
            self.assertAlmostEqual(branch[old.edge_of_bus[node]], balance, places=12)

    def test_04_recurrence_and_parameter_separation(self):
        d = self.data
        rng = np.random.RandomState(12)
        r = d.theta_nom[:d.n]
        x = d.theta_nom[d.n:]
        R = 2 * d.A.T @ np.diag(r) @ d.A
        X = 2 * d.A.T @ np.diag(x) @ d.A
        for t in (0, 8, 13, 19, 22):
            u0, u1 = rng.normal(0, .05, (2, d.nu))
            p0, q0 = d.injections(t, u0)
            p1, q1 = d.injections(t + 1, u1)
            h = d.h_external(t) + np.einsum('ijk,k->ij', d.K, u1 - u0)
            np.testing.assert_allclose(h @ d.theta_nom,
                                       (1 + R @ p1 + X @ q1) - (1 + R @ p0 + X @ q0), atol=2e-14)

    def test_05_initial_action_energy_counted_once(self):
        d, a = self.data, self.action
        verify_devices(d, 0, a, d.initial_energy)
        r = solve_step(d, 0, self.flow.voltage_sq, a.u, a.energy_after)
        verify_step(d, 0, self.flow.voltage_sq, a.u, a.energy_after, r)
        p = d.ess_params[18]
        expected = d.initial_energy[0] + d.dt * (
            p['eta_ch'] * (a.pch[0] + r.dispatch.pch[0]) -
            (a.pdis[0] + r.dispatch.pdis[0]) / p['eta_dis'])
        self.assertAlmostEqual(r.dispatch.energy_after[0], expected, places=12)

    def test_06_soft_bounds_nonzero_and_dual_absolute_agree(self):
        # 合成反馈仅用于数学压力测试；不改变真实网络/正式24时段运行。
        d, a = self.data, self.action
        digest = d.uncertainty_digest()
        for offset, side in ((.40, 'plus'), (-.40, 'minus')):
            v = self.flow.voltage_sq + offset
            dual = solve_step(d, 0, v, a.u, a.energy_after)
            absolute = solve_step(d, 0, v, a.u, a.energy_after, 'absolute')
            verify_step(d, 0, v, a.u, a.energy_after, dual)
            verify_step(d, 0, v, a.u, a.energy_after, absolute)
            self.assertGreater(getattr(dual, 'slack_' + side).sum(), 1)
            self.assertAlmostEqual(dual.info['objective'], absolute.info['objective'], places=5)
        self.assertEqual(d.uncertainty_digest(), digest)

    def test_07_ac_setpoints_voltage_and_power_balance(self):
        verify_power_flow(self.data, self.plant, self.flow)
        np.testing.assert_array_equal(self.flow.voltage_sq, self.flow.voltage_pu[1:] ** 2)
        self.assertAlmostEqual(self.flow.voltage_pu[0], 1.0)
        self.assertGreater(self.flow.loss_p_pu, 0)

    def test_08_storage_energy_limits_both_boundaries(self):
        d, a = self.data, self.action
        p = d.ess_params[18]
        for bound in ('soc_min', 'soc_max'):
            energy = np.array([p[bound] * p['e_capacity'] / d.power_base_kw])
            step = solve_step(d, 0, self.flow.voltage_sq, a.u, energy)
            verify_step(d, 0, self.flow.voltage_sq, a.u, energy, step)
            if bound == 'soc_min':
                self.assertLess(step.dispatch.pdis[0], 1e-7)
            else:
                self.assertLess(step.dispatch.pch[0], 1e-7)

    def test_09_invalid_feedback_is_rejected(self):
        d, a = self.data, self.action
        for v in (np.ones(33), np.full(32, np.nan), -np.ones(32)):
            with self.assertRaises(ValueError):
                solve_step(d, 0, v, a.u, a.energy_after)
        with self.assertRaises(ValueError):
            solve_step(d, 23, self.flow.voltage_sq, a.u, a.energy_after)
        with self.assertRaises(ValueError):
            solve_step(d, 0, self.flow.voltage_sq, a.u, np.array([-1]))

    def test_10_robust_verifier_rejects_corrupted_solution(self):
        d, a = self.data, self.action
        v = self.flow.voltage_sq + .4
        result = solve_step(d, 0, v, a.u, a.energy_after)
        result.slack_plus[:] = 0
        with self.assertRaises(AssertionError):
            verify_step(d, 0, v, a.u, a.energy_after, result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
