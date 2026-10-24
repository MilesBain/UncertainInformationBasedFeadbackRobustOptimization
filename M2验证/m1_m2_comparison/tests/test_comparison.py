"""参数漂移拦截、统一评价口径以及完整对比结果的集成校验。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared import (ROOT, ModelData, load_legacy, ACPlant, scenario, compare_scenarios,
                    verify_sources, numeric_guard_audit, digest, Dispatch)
from evaluation import evaluate
from robust_comparison import evaluate_robust
from audit_checks import audit_objective_coefficients, verify_initial_state, paired_ac_validation
from dataclasses import asdict


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = ModelData(load_legacy())
        cls.plant = ACPlant(cls.data.legacy)
        cls.scenario = scenario(cls.data, cls.plant, 120)
        cls.out = ROOT / 'results'
        if not (cls.out / 'summary.json').exists():
            raise RuntimeError('Run run_comparison.py before the result integration tests.')
        cls.summary = json.loads((cls.out / 'summary.json').read_text(encoding='utf-8'))
        cls.arrays = {}
        for name in ('M1', 'M2'):
            with np.load(cls.out / (name.lower() + '_results.npz'), allow_pickle=False) as file:
                cls.arrays[name] = {k: file[k] for k in file.files}

    def test_01_sources_and_scenario_match(self):
        verify_sources()
        other_data = ModelData(load_legacy())
        other = scenario(other_data, ACPlant(other_data.legacy), 120)
        self.assertTrue(compare_scenarios(self.scenario, other)['exact_equality'])
        self.assertEqual(digest(self.scenario), self.summary['scenario_sha256'])

    def test_02_changes_to_physical_or_model_values_are_rejected(self):
        for branch, key in [('original_parameters', 'C_V'), ('actual_controller_parameters', 'cv'),
                            ('actual_controller_parameters', 'kappa'), ('original_parameters', 'DT')]:
            other = copy.deepcopy(self.scenario)
            other[branch][key] += .001
            with self.assertRaises(AssertionError):
                compare_scenarios(self.scenario, other)
        for path in [('actual_model_arrays', 'p_load'), ('actual_model_arrays', 'p_available'),
                     ('actual_plant_input_arrays', 'p_load')]:
            other = copy.deepcopy(self.scenario)
            other[path[0]][path[1]][4][3] += .001
            with self.assertRaises(AssertionError):
                compare_scenarios(self.scenario, other)
        other = copy.deepcopy(self.scenario)
        other['true_r_pu'][0] *= 1.01
        with self.assertRaises(AssertionError):
            compare_scenarios(self.scenario, other)

    def test_03_shared_start_and_feedback_are_exact(self):
        m1, m2 = self.arrays['M1'], self.arrays['M2']
        for key in ('u', 'pch', 'pdis', 'gamma', 'voltage_pu', 'voltage_sq'):
            np.testing.assert_array_equal(m1[key][0], m2[key][0])
        np.testing.assert_array_equal(m1['energy_boundaries_pu_h'][:2], m2['energy_boundaries_pu_h'][:2])
        np.testing.assert_array_equal(m2['feedback_voltage_sq'], m2['voltage_sq'][:-1])
        np.testing.assert_array_equal(m2['feedback_control'], m2['u'][:-1])

    def test_04_no_storage_reset_and_energy_conservation(self):
        d = self.data
        for a in self.arrays.values():
            np.testing.assert_array_equal(a['energy_boundaries_pu_h'][0], d.initial_energy)
            for j, bus in enumerate(d.ess):
                cfg = d.ess_params[bus]
                change = d.dt * (cfg['eta_ch'] * a['pch'][:, j] - a['pdis'][:, j] / cfg['eta_dis'])
                np.testing.assert_allclose(np.diff(a['energy_boundaries_pu_h'][:, j]), change, atol=2e-12)
                self.assertTrue(np.all(a['pch'][:, j] * a['pdis'][:, j] < 1e-12))

    def test_05_metric_units_counts_and_common_cost(self):
        # 单个节点在单个小时1.06 p.u.，弃光10kW；手算验证单位与平方电压惩罚。
        a = {k: v.copy() for k, v in self.arrays['M1'].items()}
        a['voltage_pu'][:] = 1
        a['voltage_pu'][0, 1] = 1.06
        a['voltage_sq'] = a['voltage_pu'][:, 1:] ** 2
        a['curtailment_kw'][:] = 0
        a['curtailment_kw'][0] = 10
        metric = evaluate(self.data, a)
        self.assertEqual(metric['voltage_violation_node_periods'], 1)
        self.assertEqual(metric['voltage_violation_periods'], 1)
        self.assertAlmostEqual(metric['curtailment_kwh'], 10)
        self.assertAlmostEqual(metric['common_ac_cost'], 100 * (1.06 ** 2 - 1.05 ** 2) + 10 / 10000)
        post = evaluate(self.data, a, start=1)
        self.assertEqual(post['voltage_violation_node_periods'], 0)
        self.assertEqual(post['curtailment_kwh'], 0)

    def test_06_same_dispatch_has_identical_evaluation(self):
        a = self.arrays['M1']
        self.assertEqual(evaluate(self.data, a), evaluate(self.data, copy.deepcopy(a)))
        for method in ('M1', 'M2'):
            metric = evaluate(self.data, self.arrays[method])
            self.assertEqual(metric, self.summary['metrics'][method]['full_day'])

    def test_07_every_saved_array_is_finite(self):
        for a in self.arrays.values():
            for key, arr in a.items():
                self.assertTrue(np.isfinite(arr).all(), key)
            self.assertEqual(a['u'].shape, (24, 16))
            self.assertEqual(a['voltage_pu'].shape, (24, 33))
            self.assertEqual(a['energy_boundaries_pu_h'].shape, (25, 1))
            np.testing.assert_array_equal(a['voltage_sq'], a['voltage_pu'][:, 1:] ** 2)

    def test_08_all_periods_have_parameter_audits(self):
        audit = json.loads((self.out / 'fairness_audit.json').read_text(encoding='utf-8'))
        self.assertEqual(len(audit['per_period_unchanged']), 23)
        for row in audit['per_period_unchanged']:
            self.assertEqual(row['m1_sha256'], audit['m1_sha256'])
            self.assertEqual(row['m2_sha256'], audit['m1_sha256'])
        self.assertTrue(numeric_guard_audit(self.data)['legacy_numeric_guards_redundant_for_static_model'])

    def test_09_original_M2_trajectory_is_preserved(self):
        original = ROOT.parent / 'm2_rolling' / 'results' / 'rolling_arrays.npz'
        if not original.exists():
            self.skipTest('Original project results not present in this standalone copy.')
        with np.load(original, allow_pickle=False) as old:
            np.testing.assert_allclose(self.arrays['M2']['u'], old['u'], rtol=0, atol=2e-12)
            np.testing.assert_allclose(self.arrays['M2']['voltage_pu'], old['voltage_pu'], rtol=0, atol=2e-12)

    def action(self, method, t):
        a = self.arrays[method]
        return Dispatch(*(a[key][t].copy() for key in ('u', 'pch', 'pdis', 'zch', 'zdis', 'gamma')),
                        a['energy_boundaries_pu_h'][t + 1].copy())

    def test_10_initial_all_fields_and_drift_rejection(self):
        d2 = ModelData(load_legacy())
        plant2 = ACPlant(d2.legacy)
        a1, a2 = self.action('M1', 0), self.action('M2', 0)
        f1, f2, _, _, record = paired_ac_validation(self.data, d2, self.plant, plant2, 0, a1, a2)
        verify_initial_state(self.data, d2, a1, a2, f1, f2)
        for key in ('u', 'pch', 'pdis', 'zch', 'zdis', 'gamma', 'energy_after'):
            changed = copy.deepcopy(a2)
            getattr(changed, key)[0] += .001
            with self.assertRaises(AssertionError):
                verify_initial_state(self.data, d2, a1, changed, f1, f2)

    def test_11_both_control_sets_have_real_ac_results(self):
        records = json.loads((self.out / 'paired_ac_audit.json').read_text(encoding='utf-8'))
        self.assertEqual(len(records), 24)
        for t, record in enumerate(records):
            self.assertEqual(record['period'], t)
            self.assertEqual(record['M1_environment_sha256'], record['M2_environment_sha256'])
            for method in ('M1', 'M2'):
                self.assertEqual(record[method + '_voltage_source'], 'PYPOWER.runpf')
                self.assertEqual(record[method + '_ac_voltage_sha256'], digest(self.arrays[method]['voltage_sq'][t]))
                self.assertEqual(record[method + '_control_sha256'], digest(asdict(self.action(method, t))))
        # 中午分别重放两套保存的控制；若把预测当AC值，该检查会失败。
        d2 = ModelData(load_legacy())
        f1, f2, _, _, _ = paired_ac_validation(self.data, d2, self.plant, ACPlant(d2.legacy),
                                               14, self.action('M1', 14), self.action('M2', 14))
        for method, flow in (('M1', f1), ('M2', f2)):
            np.testing.assert_allclose(flow.voltage_sq, self.arrays[method]['voltage_sq'][14], rtol=0, atol=1e-12)
            self.assertGreater(np.max(abs(flow.voltage_sq - self.arrays[method]['nominal_sq'][14])), 1e-4)

    def test_12_prediction_metrics_use_correct_window_and_arrays(self):
        for method in ('M1', 'M2'):
            actual = evaluate_robust(self.data, self.arrays[method], 1)
            self.assertEqual(actual, self.summary['robust_prediction_metrics'][method]['comparison_periods_1_to_23'])
            altered = copy.deepcopy(self.arrays[method])
            altered['lower_sq'][0] = -100
            altered['upper_sq'][0] = 100
            altered['model_objective'][0] = 1e6
            self.assertEqual(evaluate_robust(self.data, altered, 1), actual)
            altered = copy.deepcopy(self.arrays[method])
            altered['voltage_sq'][1:] = altered['nominal_sq'][1:]
            refreshed = evaluate_robust(self.data, altered, 1)
            self.assertEqual(refreshed['predicted_robust_objective_sum'], actual['predicted_robust_objective_sum'])
            self.assertEqual(refreshed['mean_interval_width_sq'], actual['mean_interval_width_sq'])
            self.assertAlmostEqual(refreshed['nominal_vs_ac_rmse_sq'], 0)

    def test_13_actual_M1_objective_coefficients_are_checked(self):
        d2 = ModelData(load_legacy())
        audit_objective_coefficients(self.data, d2)
        original = self.data.legacy.b.obj.copy()
        try:
            key = next(iter(self.data.legacy.b.obj))
            self.data.legacy.b.obj[key] += .001
            with self.assertRaises(AssertionError):
                audit_objective_coefficients(self.data, d2)
        finally:
            self.data.legacy.b.obj = original


if __name__ == '__main__':
    unittest.main(verbosity=2)
