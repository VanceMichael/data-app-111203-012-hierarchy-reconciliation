"""滚动原点验证。

每个折叠只用不晚于预测起点的观测与当时生效的结构（parent_map 只取
effective_week <= origin 的边），因此不会偷看未来归属。同时报告两类
口径并显式展示取舍：

* 站点误差：depot 级 MAE / RMSE / 平均偏差（排班的个体精度）；
* 区域资源偏差：按实现周真实结构汇总后的区域总量 MAE / 偏差，
  衡量区域排班资源是过剩还是不足。
"""
import math
from statistics import fmean

from .forecast import run_forecast
from .structure import ROOT


def _metrics(errors):
    if not errors:
        return {'n': 0, 'mae': None, 'rmse': None, 'mean_bias': None}
    return {'n': len(errors),
            'mae': fmean(abs(e) for e in errors),
            'rmse': math.sqrt(fmean(e * e for e in errors)),
            'mean_bias': fmean(errors)}


def rolling_validation(records, structure, origins, horizon=1):
    actual = {}
    closed_actual = {}
    for row in records:
        actual[(row['depot'], row['week'])] = row['hours']
        closed_actual[(row['depot'], row['week'])] = row['closed']
    depot_errors = {'base': [], 'reconciled': [], 'reconciled_unconstrained': []}
    region_bias = {'base': [], 'reconciled': [], 'reconciled_unconstrained': []}
    national_bias = {'base': [], 'reconciled': []}
    folds = []
    for origin in origins:
        result = run_forecast(records, structure, origin, horizon)
        target_week = origin + 1
        realized_snap, realized_s = structure.summing_matrix(target_week)
        week_result = result['weeks'][0]
        fold = {'origin': origin, 'target_week': target_week,
                'structure_changed': target_week in {e['week'] for e in result['audit']['mapping_events']},
                'depot': {}, 'region': {}, 'national': {},
                'conservation': week_result['conservation']}
        realized_depots = sorted({leaf for leaf in realized_snap['leaves']
                                  if (leaf, target_week) in actual})
        for variant in depot_errors:
            errors = []
            for depot in realized_depots:
                predicted = week_result[variant].get(depot)
                if predicted is None:
                    continue
                truth = actual[(depot, target_week)]
                errors.append(predicted - truth)
                fold['depot'].setdefault(variant, {})[depot] = {
                    'predicted': predicted, 'actual': truth, 'error': predicted - truth}
            depot_errors[variant].extend(errors)
        for variant in region_bias:
            biases = []
            for region, members in realized_snap['children'].items():
                if region == ROOT:
                    continue
                leaves = [d for d in realized_snap['leaves']
                          if _under(realized_snap['parent_of'], d, region)]
                truth = sum(actual.get((d, target_week), 0.0) for d in leaves)
                predicted = week_result[variant].get(region)
                if predicted is None:
                    predicted = sum(week_result[variant].get(d, 0.0) for d in leaves)
                biases.append(predicted - truth)
                fold['region'].setdefault(variant, {})[region] = {
                    'predicted': predicted, 'actual': truth, 'bias': predicted - truth}
            region_bias[variant].extend(biases)
        for variant in national_bias:
            truth = sum(actual.get((d, target_week), 0.0) for d in realized_depots)
            predicted = week_result[variant][ROOT]
            national_bias[variant].append(predicted - truth)
            fold['national'][variant] = {'predicted': predicted, 'actual': truth,
                                         'bias': predicted - truth}
        folds.append(fold)
    summary = {
        'depot_error': {variant: _metrics(values) for variant, values in depot_errors.items()},
        'region_resource_bias': {variant: _metrics(values) for variant, values in region_bias.items()},
        'national_bias': {variant: _metrics(values) for variant, values in national_bias.items()}}
    tradeoff = _tradeoff(summary)
    return {'folds': folds, 'summary': summary, 'tradeoff': tradeoff,
            'rule': '仅使用预测起点前可见的结构与观测；区域真值按实现周 as-was 汇总'}


def _under(parent_of, leaf, node):
    current = leaf
    while current != node:
        if current not in parent_of:
            return False
        current = parent_of[current]
    return True


def _tradeoff(summary):
    base_depot = summary['depot_error']['base']
    rec_depot = summary['depot_error']['reconciled']
    base_region = summary['region_resource_bias']['base']
    rec_region = summary['region_resource_bias']['reconciled']
    return {
        'depot_mae_change': rec_depot['mae'] - base_depot['mae'],
        'region_mae_change': rec_region['mae'] - base_region['mae'],
        'region_mean_bias_base': base_region['mean_bias'],
        'region_mean_bias_reconciled': rec_region['mean_bias'],
        'interpretation': (
            'depot_mae_change > 0 表示协调牺牲了部分站点个体精度；'
            'region_mae_change < 0 表示区域资源总量偏差被压低。'
            '排班按区域总量备料时优先区域口径，按站点派工时优先站点口径，两者不可同时最优。')}
