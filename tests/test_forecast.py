import csv
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analysis_app import linalg
from analysis_app.forecast import base_forecast, error_covariance, run_forecast
from analysis_app.history import rebuild
from analysis_app.structure import ROOT, load_structure
from analysis_app.validation import rolling_validation

FIELDNAMES = ['observation_id', 'depot', 'region', 'week', 'hours',
              'crew', 'closed', 'structure_version']


def write_csv(path, header, rows):
    with Path(path).open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def make_observations(directory, depot_region_weeks, hours_map=None, closed_depot_week=None,
                      version_map=None):
    """depot_region_weeks: {depot: {week: region}}。"""
    hours_map = hours_map or {}
    closed_depot_week = closed_depot_week or set()
    version_map = version_map or {}
    rows = []
    index = 0
    for depot in sorted(depot_region_weeks):
        for week in sorted(depot_region_weeks[depot]):
            hours = hours_map.get((depot, week))
            if hours is None:
                hours = round(20 + 10 * ((week + index) % 5) + (index % 3), 4)
            rows.append({'observation_id': f'obs-{index:03d}', 'depot': depot,
                         'region': depot_region_weeks[depot][week], 'week': week,
                         'hours': hours, 'crew': 2,
                         'closed': 1 if (depot, week) in closed_depot_week else 0,
                         'structure_version': version_map.get(week, 'v1')})
            index += 1
    path = Path(directory) / 'observations.csv'
    write_csv(path, FIELDNAMES, rows)
    return path


def write_structure(directory, edges, closures=None, mappings=None):
    structure_path = Path(directory) / 'structure.csv'
    write_csv(structure_path, ['node', 'parent', 'effective_week'], edges)
    closure_path = Path(directory) / 'closures.csv'
    write_csv(closure_path, ['node', 'closed_week'], closures or [])
    mapping_path = Path(directory) / 'node_mappings.csv'
    rows = mappings if mappings else []
    write_csv(mapping_path, ['source_node', 'target_node', 'effective_week', 'strategy'], rows)
    return structure_path, closure_path, mapping_path


def simple_edges(region_of_depot, regions=('R0', 'R1')):
    edges = [{'node': region, 'parent': ROOT, 'effective_week': 0} for region in regions]
    for depot, region in sorted(region_of_depot.items()):
        edges.append({'node': depot, 'parent': region, 'effective_week': 0})
    return edges


class StructureValidationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_duplicate_parent_same_week_rejected(self):
        edges = simple_edges({'DA': 'R0'})
        edges.append({'node': 'DA', 'parent': 'R1', 'effective_week': 0})
        structure_path, closure_path, mapping_path = write_structure(self.tmp, edges)
        with self.assertRaisesRegex(ValueError, '两个父节点'):
            load_structure(structure_path, closure_path, mapping_path)

    def test_cycle_rejected(self):
        edges = [{'node': 'R0', 'parent': ROOT, 'effective_week': 0},
                 {'node': 'DA', 'parent': 'R0', 'effective_week': 0},
                 {'node': 'R0', 'parent': 'DA', 'effective_week': 3}]
        structure_path, closure_path, mapping_path = write_structure(self.tmp, edges)
        with self.assertRaisesRegex(ValueError, '成环'):
            load_structure(structure_path, closure_path, mapping_path)

    def test_self_loop_and_duplicate_attachment_rejected(self):
        edges = simple_edges({'DA': 'R0'})
        edges.append({'node': 'DA', 'parent': 'R0', 'effective_week': 0})
        structure_path, closure_path, mapping_path = write_structure(self.tmp, edges)
        with self.assertRaisesRegex(ValueError, '重复挂接'):
            load_structure(structure_path, closure_path, mapping_path)

    def test_unknown_parent_rejected(self):
        edges = [{'node': 'R0', 'parent': ROOT, 'effective_week': 0},
                 {'node': 'DA', 'parent': 'GHOST', 'effective_week': 0}]
        structure_path, closure_path, mapping_path = write_structure(self.tmp, edges)
        with self.assertRaisesRegex(ValueError, '未连到根'):
            load_structure(structure_path, closure_path, mapping_path)

    def test_unknown_mapping_strategy_rejected(self):
        edges = simple_edges({'DA': 'R0', 'DB': 'R1'})
        mappings = [{'source_node': 'DA', 'target_node': 'DB',
                     'effective_week': 4, 'strategy': 'future_rewrite'}]
        structure_path, closure_path, mapping_path = write_structure(self.tmp, edges, mappings=mappings)
        with self.assertRaisesRegex(ValueError, '未知映射策略'):
            load_structure(structure_path, closure_path, mapping_path)

    def test_snapshot_parent_changes_with_effective_week(self):
        edges = simple_edges({'DA': 'R0', 'DB': 'R1'})
        edges.append({'node': 'DA', 'parent': 'R1', 'effective_week': 5})
        structure_path, closure_path, mapping_path = write_structure(self.tmp, edges)
        structure = load_structure(structure_path, closure_path, mapping_path)
        self.assertEqual(structure.parent_map(4)['DA'], 'R0')
        self.assertEqual(structure.parent_map(5)['DA'], 'R1')
        events = structure.mapping_events()
        self.assertEqual([e for e in events if e['node'] == 'DA'][0]['strategy'], 'as_was')


class RegionSplitTest(unittest.TestCase):
    """拆区：DA 第 5 周从 R0 调到 R1，历史归属不得被回填。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)
        depot_weeks = {}
        for depot in ('DA', 'DB', 'DC'):
            depot_weeks[depot] = {w: ('R1' if depot in {'DB'} or (depot == 'DA' and w >= 5) else 'R0')
                                  for w in range(10)}
        self.records_path = make_observations(self.tmp, depot_weeks)
        edges = simple_edges({'DA': 'R0', 'DB': 'R1', 'DC': 'R0'})
        edges.append({'node': 'DA', 'parent': 'R1', 'effective_week': 5})
        self.structure_path, self.closure_path, self.mapping_path = write_structure(self.tmp, edges)
        self.structure = load_structure(self.structure_path, self.closure_path, self.mapping_path)
        from analysis_app.io import load_records
        self.records = load_records(self.records_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_history_keeps_as_was_attribution(self):
        series, audit = rebuild(self.records, self.structure, origin=9)
        r0_before = next(e for e in series['R0'] if e['week'] == 3)
        r1_before = next(e for e in series['R1'] if e['week'] == 3)
        da_before = next(e for e in series['DA'] if e['week'] == 3)['hours']
        self.assertEqual(r0_before['members'].count('DA'), 1)
        self.assertNotIn('DA', r1_before['members'])
        r1_after = next(e for e in series['R1'] if e['week'] == 6)
        self.assertIn('DA', r1_after['members'])
        national = sum(e['hours'] for e in series[ROOT] if e['hours'] is not None)
        depots = sum(e['hours'] for depot in ('DA', 'DB', 'DC')
                     for e in series[depot] if e['hours'] is not None)
        self.assertAlmostEqual(national, depots)
        self.assertTrue(any(event['week'] == 5 and event['node'] == 'DA'
                            for event in audit['mapping_events']))

    def test_origin_before_change_cannot_see_future_structure(self):
        result = run_forecast(self.records, self.structure, origin=4, horizon=1)
        snap, _ = self.structure.summing_matrix(4)
        self.assertEqual(snap['parent_of']['DA'], 'R0')
        week = result['weeks'][0]
        self.assertAlmostEqual(week['reconciled'][ROOT],
                               week['reconciled']['R0'] + week['reconciled']['R1'])
        self.assertAlmostEqual(week['reconciled']['R0'],
                               sum(week['leaf_solution'][d] for d in snap['children']['R0']))

    def test_reconciliation_conserves_and_stays_nonneg(self):
        result = run_forecast(self.records, self.structure, origin=9, horizon=3)
        for week in result['weeks']:
            self.assertTrue(week['conservation']['all_nonneg'])
            self.assertLess(week['conservation']['max_parent_gap'], 1e-9)
            self.assertGreaterEqual(week['conservation']['min_value'], -1e-12)
            self.assertAlmostEqual(week['reconciled'][ROOT],
                                   week['reconciled']['R0'] + week['reconciled']['R1'])

    def test_rolling_validation_flags_change_and_reports_tradeoff(self):
        report = rolling_validation(self.records, self.structure, origins=[3, 4, 5, 6])
        changed = [fold for fold in report['folds'] if fold['structure_changed']]
        self.assertTrue(any(fold['target_week'] == 5 for fold in changed))
        self.assertIn('depot_mae_change', report['tradeoff'])
        self.assertIn('region_mae_change', report['tradeoff'])


class DepotMergeTest(unittest.TestCase):
    """并站：DA 第 6 周并入 DB（merge_series），其后不再作为在册叶节点。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)
        depot_weeks = {'DA': {w: 'R0' for w in range(6)},
                       'DB': {w: 'R0' for w in range(10)},
                       'DC': {w: 'R1' for w in range(10)}}
        self.records_path = make_observations(self.tmp, depot_weeks)
        edges = simple_edges({'DA': 'R0', 'DB': 'R0', 'DC': 'R1'})
        mappings = [{'source_node': 'DA', 'target_node': 'DB',
                     'effective_week': 6, 'strategy': 'merge_series'}]
        paths = write_structure(self.tmp, edges, mappings=mappings)
        self.structure_path, self.closure_path, self.mapping_path = paths
        self.structure = load_structure(*paths)
        from analysis_app.io import load_records
        self.records = load_records(self.records_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_merged_source_leaves_leaf_set_after_effective_week(self):
        before = self.structure.snapshot(5)
        after = self.structure.snapshot(6)
        self.assertIn('DA', before['leaves'])
        self.assertNotIn('DA', after['leaves'])
        self.assertIn('DA', after['merged_away'])
        self.assertNotIn('DA', after['ordered'])

    def test_merge_series_is_traced_and_does_not_double_count(self):
        series, audit = rebuild(self.records, self.structure, origin=9)
        db_entry = next(e for e in series['DB'] if e['week'] == 2)
        da_entry = next(e for e in series['DA'] if e['week'] == 2)
        self.assertTrue(any(c['source'] == 'DA' for c in db_entry.get('merged_from', [])))
        raw_db = next(r['hours'] for r in self.records
                      if r['depot'] == 'DB' and r['week'] == 2)
        self.assertAlmostEqual(db_entry['hours'], raw_db + da_entry['hours'])
        self.assertTrue(any(item['source'] == 'DA' for item in audit['merge_contributions']))
        result = run_forecast(self.records, self.structure, origin=9, horizon=2)
        for week in result['weeks']:
            leaves = result['structure']['leaves']
            self.assertNotIn('DA', leaves)
            self.assertAlmostEqual(week['reconciled'][ROOT],
                                   sum(week['leaf_solution'].values()))

    def test_drop_after_merge_strategy_removes_without_series_rewrite(self):
        mappings = [{'source_node': 'DA', 'target_node': 'DB',
                     'effective_week': 6, 'strategy': 'drop_after_merge'}]
        paths = write_structure(self.tmp, simple_edges({'DA': 'R0', 'DB': 'R0', 'DC': 'R1'}),
                                mappings=mappings)
        structure = load_structure(*paths)
        series, audit = rebuild(self.records, structure, origin=9)
        db_entry = next(e for e in series['DB'] if e['week'] == 2)
        self.assertNotIn('merged_from', db_entry)
        self.assertEqual(audit['merge_contributions'], [])


class AllZeroHistoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)
        depot_weeks = {depot: {w: 'R0' if depot != 'DZ' else 'R1' for w in range(8)}
                       for depot in ('DN', 'DZ')}
        hours = {('DZ', w): 0.0 for w in range(8)}
        self.records_path = make_observations(self.tmp, depot_weeks, hours_map=hours)
        paths = write_structure(self.tmp, simple_edges({'DN': 'R0', 'DZ': 'R1'}))
        self.structure = load_structure(*paths)
        from analysis_app.io import load_records
        self.records = load_records(self.records_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_zero_series_forecasts_zero_and_gets_variance_floor(self):
        series, _ = rebuild(self.records, self.structure, origin=7)
        fit = base_forecast(series['DZ'], horizon=2)
        self.assertEqual(fit['forecast'], [0.0, 0.0])
        w, diagnostics = error_covariance(['DN', 'DZ'],
                                          {node: base_forecast(series[node], 2) for node in ('DN', 'DZ')})
        self.assertIn('DZ', diagnostics['zero_variance_nodes'])
        self.assertTrue(linalg.is_positive_definite(w))

    def test_full_run_conserves_with_zero_node(self):
        result = run_forecast(self.records, self.structure, origin=7, horizon=2)
        for week in result['weeks']:
            self.assertEqual(week['reconciled']['DZ'], 0.0)
            self.assertAlmostEqual(week['reconciled'][ROOT],
                                   week['reconciled']['R0'] + week['reconciled']['R1'])
            self.assertTrue(week['conservation']['all_nonneg'])


class IllConditionedCovarianceTest(unittest.TestCase):
    def test_perfectly_correlated_residuals_are_regularized(self):
        # 三个节点残差完全成比例：样本协方差秩为 1，是真实的奇异矩阵
        fits = {node: {'residuals': {w: scale * (1.0 + (w % 2))
                                     for w in range(6)}, 'short_history': False}
                for node, scale in (('DA', 1.0), ('DB', 2.0), ('DC', -3.0))}
        sample = []
        nodes = list(fits)
        for a in nodes:
            row = []
            for b in nodes:
                va = list(fits[a]['residuals'].values())
                vb = list(fits[b]['residuals'].values())
                ma, mb = sum(va) / len(va), sum(vb) / len(vb)
                row.append(sum((x - ma) * (y - mb) for x, y in zip(va, vb)) / (len(va) - 1))
            sample.append(row)
        self.assertFalse(linalg.is_positive_definite(sample))
        w, diagnostics = error_covariance(nodes, fits)
        self.assertFalse(diagnostics['raw_positive_definite'])
        # 完全成比例时收缩目标方差为 0，岭兜底；近共线场景则触发正收缩
        self.assertTrue(diagnostics['ridge'] > 0.0)
        self.assertTrue(linalg.is_positive_definite(w))
        self.assertIsNotNone(linalg.symmetric_inverse(w))
        near = {'DA': {'residuals': {0: 1.0, 1: -1.0, 2: 0.5}, 'short_history': False},
                'DB': {'residuals': {0: 0.8, 1: -1.2, 2: 0.7}, 'short_history': False}}
        _, diag_near = error_covariance(['DA', 'DB'], near)
        self.assertGreater(diag_near['shrinkage'], 0.0)
        self.assertLess(diag_near['shrinkage'], 1.0)

    def test_short_history_node_marked_and_kept_finite(self):
        fits = {'DA': {'residuals': {0: 2.0}, 'short_history': True},
                'DB': {'residuals': {w: (-1) ** w for w in range(6)}, 'short_history': False}}
        w, diagnostics = error_covariance(['DA', 'DB'], fits)
        self.assertIn('DA', diagnostics['short_history_nodes'])
        self.assertTrue(all(value == value and abs(value) != float('inf')
                            for row in w for value in row))


class NonNegativityTest(unittest.TestCase):
    """构造会让无约束 MinT 出现负值的场景，验证约束在优化器内生效。"""

    def test_active_set_qp_matches_grid_optimum(self):
        # 无约束最优解 x0* 为负，x0=0 必须进入主动集
        q = [[4.0, 1.0], [1.0, 2.0]]
        b = [-1.0, 3.0]
        x, info = linalg.nonnegative_qp(q, b, fixed_zero=[])
        best = None
        for x0 in [i * 0.05 for i in range(120)]:
            for x1 in [i * 0.05 for i in range(120)]:
                objective = 0.5 * (4 * x0 ** 2 + 2 * x0 * x1 + 2 * x1 ** 2) + x0 - 3 * x1
                if best is None or objective < best[0]:
                    best = (objective, (x0, x1))
        for index in range(2):
            self.assertAlmostEqual(x[index], best[1][index], places=1)
        self.assertIn(0, info['active_nonneg'])
        self.assertEqual(x[0], 0.0)

    def test_fixed_zero_cannot_be_released(self):
        q = [[2.0, 0.0], [0.0, 2.0]]
        b = [5.0, 5.0]
        x, info = linalg.nonnegative_qp(q, b, fixed_zero=[0])
        self.assertEqual(x[0], 0.0)
        self.assertEqual(info['fixed_zero'], [0])
        self.assertAlmostEqual(x[1], 2.5)

    def test_random_qp_matches_exhaustive_active_sets(self):
        rng = random.Random(2026)
        for trial in range(40):
            n = 4
            raw = [[rng.gauss(0, 1) for _ in range(n)] for _ in range(n)]
            q = [[sum(raw[k][i] * raw[k][j] for k in range(n))
                  + (1.0 if i == j else 0.0) for j in range(n)] for i in range(n)]
            b = [rng.gauss(0, 4) for _ in range(n)]
            x, info = linalg.nonnegative_qp(q, b, fixed_zero=[])
            objective = 0.5 * sum(q[i][j] * x[i] * x[j] for i in range(n) for j in range(n)) \
                - sum(b[i] * x[i] for i in range(n))
            # 枚举所有主动集（2^n），每个等式面求无约束解并检查可行性
            best = float('inf')
            for mask in range(1 << n):
                free = [i for i in range(n) if not (mask >> i) & 1]
                if not free:
                    candidate_obj = 0.0
                else:
                    qff = linalg.submatrix(q, free, free)
                    try:
                        y = linalg.solve(qff, [b[i] for i in free])
                    except ValueError:
                        continue
                    if any(value < -1e-8 for value in y):
                        continue
                    full = [0.0] * n
                    for i, value in zip(free, y):
                        full[i] = value
                    candidate_obj = 0.5 * sum(q[i][j] * full[i] * full[j]
                                              for i in range(n) for j in range(n)) \
                        - sum(b[i] * full[i] for i in range(n))
                best = min(best, candidate_obj)
            self.assertTrue(all(value >= -1e-9 for value in x))
            self.assertAlmostEqual(objective, best, places=7)

    def test_negative_base_is_reconciled_not_clipped(self):
        # 端到端：基准预测按业务口径非负，约束 QP 仍需保证 MinT 再分配不产生负值
        directory = tempfile.TemporaryDirectory()
        tmp = Path(directory.name)
        depot_weeks = {'DA': {w: 'R0' for w in range(8)},
                       'DB': {w: 'R0' for w in range(8)}}
        rng = random.Random(7)
        hours = {}
        for w in range(8):
            hours[('DA', w)] = max(0.0, 30 + 8 * rng.random())
            hours[('DB', w)] = 0.0 if w < 7 else 0.5
        records_path = make_observations(tmp, depot_weeks, hours_map=hours)
        paths = write_structure(tmp, simple_edges({'DA': 'R0', 'DB': 'R0'}))
        from analysis_app.io import load_records
        records = load_records(records_path)
        structure = load_structure(*paths)
        result = run_forecast(records, structure, origin=7, horizon=1)
        week = result['weeks'][0]
        self.assertTrue(all(v >= 0 for v in week['reconciled'].values()))
        self.assertAlmostEqual(week['reconciled']['R0'],
                               sum(week['leaf_solution'].values()))
        directory.cleanup()

    def test_qp_nonnegativity_differs_from_naive_clipping(self):
        # 负基准来自层级再分配：直接调 reconcile 的二次问题，
        # 证明约束解会重新优化其它叶，而不是把负值简单裁零
        s_matrix = [[1.0, 1.0], [1.0, 0.0], [0.0, 1.0]]
        weight = [[1.0, 0.1, 0.1], [0.1, 2.0, 0.0], [0.1, 0.0, 3.0]]
        w_inv = linalg.symmetric_inverse(weight)
        st = linalg.transpose(s_matrix)
        q = linalg.matmul(linalg.matmul(st, w_inv), s_matrix)
        base = [10.0, -2.0, 12.0]
        c = linalg.matvec(linalg.matmul(st, w_inv), base)
        y_free = linalg.solve(q, c)
        self.assertTrue(any(v < 0 for v in y_free))
        y_qp, info = linalg.nonnegative_qp(q, c, fixed_zero=[])
        self.assertIn(0, info['active_nonneg'])
        self.assertEqual(y_qp[0], 0.0)
        self.assertTrue(all(v >= 0 for v in y_qp))
        clipped = [max(0.0, v) for v in y_free]
        self.assertNotAlmostEqual(sum(clipped), sum(y_qp))
        self.assertAlmostEqual(y_qp[1], 10.548089591567852)


class ClosedNodeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)
        depot_weeks = {'DA': {w: 'R0' for w in range(12)},
                       'DB': {w: 'R1' for w in range(12)}}
        closed = {('DB', w) for w in (10, 11)}
        self.records_path = make_observations(self.tmp, depot_weeks, closed_depot_week=closed)
        paths = write_structure(self.tmp, simple_edges({'DA': 'R0', 'DB': 'R1'}),
                                closures=[{'node': 'DB', 'closed_week': 10}])
        self.structure = load_structure(*paths)
        from analysis_app.io import load_records
        self.records = load_records(self.records_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_closed_node_pinned_to_zero_in_horizon(self):
        result = run_forecast(self.records, self.structure, origin=11, horizon=2)
        self.assertIn('DB', result['structure']['fixed_zero'])
        for week in result['weeks']:
            self.assertEqual(week['leaf_solution']['DB'], 0.0)
            self.assertIn('DB', week['constrained']['fixed_zero'])
            self.assertTrue(week['conservation']['all_nonneg'])

    def test_closed_node_before_closure_week_is_normal(self):
        result = run_forecast(self.records, self.structure, origin=8, horizon=1)
        self.assertNotIn('DB', result['structure']['fixed_zero'])
        self.assertGreater(result['weeks'][0]['reconciled']['DB'], 0.0)


class DemoCliTest(unittest.TestCase):
    def test_demo_writes_scheduling_json_and_passes_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'schedule.json'
            run = subprocess.run([sys.executable, '-m', 'analysis_app', 'demo',
                                  '--validate', '--output', str(output)],
                                 text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            payload = json.loads(output.read_text())
            self.assertEqual(payload['root'], ROOT)
            for week in payload['weeks']:
                self.assertTrue(week['conservation']['all_nonneg'])
                self.assertEqual(week['conservation']['max_parent_gap'], 0.0)
                self.assertIn('D23', week['fixed_zero'])
            self.assertIn('滚动验证', run.stdout)

    def test_forecast_command_outputs_full_document(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'forecast.json'
            run = subprocess.run([sys.executable, '-m', 'analysis_app', 'forecast',
                                  '--origin', '9', '--horizon', '2', '--output', str(output)],
                                 text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            document = json.loads(output.read_text())
            self.assertEqual(document['schema_version'], 2)
            self.assertEqual(len(document['weeks']), 2)
            self.assertIn('error_covariance', document)


if __name__ == '__main__':
    unittest.main()
