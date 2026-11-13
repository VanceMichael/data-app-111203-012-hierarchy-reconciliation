"""各层独立基准预测与层级协调。

流程分两步，严格区分信息来源：

1. 基准预测（base）：全国、区域、站点各自在 as-was 重建后的序列上
   独立建模（阻尼 Holt，短历史退化为扩展均值），层与层之间不共享结果，
   因此可以独立比较各层精度，也天然可能对不上。
2. 协调（reconcile）：只使用训练窗口内的滚动一步预测残差估计误差
   协方差 W（Schäfer–Strimmer 收缩 + 岭，处理奇异/病态与短历史），
   先解无约束 MinT，再在同一二次目标上做带非负与停用约束的主动集 QP。
   非负性是优化器内部约束，不是把负解事后裁零。
"""
from . import linalg
from .history import rebuild
from .structure import ROOT

ALPHA = 0.6
BETA = 0.2
PHI = 0.9
MIN_HISTORY = 4
VARIANCE_FLOOR = 1e-6
RIDGE_SCALE = 1e-6


def _observed(entries):
    return [(entry['week'], entry['hours']) for entry in entries
            if entry['hours'] is not None]


def base_forecast(entries, horizon):
    """返回 {'forecast': [...], 'residuals': {week: err}, 'kind': ...}。

    停用节点（最后一个观测周 closed=1）基准预测恒为 0；
    全零或极短历史走均值法并打上标记，由协方差层加方差下界。
    """
    observed = _observed(entries)
    closed = bool(entries and entries[-1].get('closed') == 1)
    if closed:
        residuals = _holt_residuals(observed) if len(observed) >= 2 else {}
        return {'forecast': [0.0] * horizon, 'residuals': residuals,
                'kind': 'closed_zero', 'short_history': len(observed) < MIN_HISTORY}
    if len(observed) < 2:
        value = observed[0][1] if observed else 0.0
        return {'forecast': [max(0.0, value)] * horizon, 'residuals': {},
                'kind': 'short_mean', 'short_history': True}
    if len(observed) < MIN_HISTORY:
        return _mean_forecast(observed, horizon)
    return _holt_forecast(observed, horizon)


def _mean_forecast(observed, horizon):
    residuals = {}
    total = 0.0
    for index, (week, value) in enumerate(observed):
        if index >= 1:
            residuals[week] = value - total / index
        total += value
    mean = total / len(observed)
    return {'forecast': [max(0.0, mean)] * horizon, 'residuals': residuals,
            'kind': 'expanding_mean', 'short_history': True}


def _holt_state(observed):
    level = observed[0][1]
    trend = observed[1][1] - observed[0][1]
    residuals = {}
    for index, (week, value) in enumerate(observed):
        if index >= 1:
            fitted = level + PHI * trend
            residuals[week] = value - fitted
            prev_level, prev_trend = level, trend
            level = ALPHA * value + (1 - ALPHA) * (prev_level + PHI * prev_trend)
            trend = BETA * (level - prev_level) + (1 - BETA) * PHI * prev_trend
    return level, trend, residuals


def _holt_residuals(observed):
    return _holt_state(observed)[2]


def _holt_forecast(observed, horizon):
    level, trend, residuals = _holt_state(observed)
    forecast = []
    damping = 0.0
    for step in range(1, horizon + 1):
        damping = PHI * (damping + 1) if step > 1 else PHI
        forecast.append(max(0.0, level + damping * trend))
    return {'forecast': forecast, 'residuals': residuals,
            'kind': 'damped_holt', 'short_history': False}


def error_covariance(nodes, fits):
    """仅用训练窗残差构造收缩后的误差协方差，返回 (W, diagnostics)。"""
    raw = {node: fits[node]['residuals'] for node in nodes}
    short = sorted(node for node in nodes if fits[node].get('short_history'))
    weeks = sorted({week for mapping in raw.values() for week in mapping})
    means = {}
    for node in nodes:
        values = list(raw[node].values())
        means[node] = sum(values) / len(values) if values else 0.0

    def pair_values(a, b):
        common = sorted(set(raw[a]) & set(raw[b]))
        return [(raw[a][w] - means[a], raw[b][w] - means[b]) for w in common]

    n = len(nodes)
    sample = [[0.0] * n for _ in range(n)]
    zero_variance = []
    for i, a in enumerate(nodes):
        pairs = pair_values(a, a)
        if len(pairs) >= 2:
            sample[i][i] = sum(x * x for x, _ in pairs) / (len(pairs) - 1)
        if sample[i][i] <= VARIANCE_FLOOR:
            zero_variance.append(a)
            sample[i][i] = max(sample[i][i], VARIANCE_FLOOR)
    pi_sum = target_sum = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            pairs = pair_values(nodes[i], nodes[j])
            if len(pairs) >= 2:
                count = len(pairs)
                sij = sum(x * y for x, y in pairs) / (count - 1)
                sample[i][j] = sample[j][i] = sij
                # Schäfer–Strimmer (2005) 的方差估计，构造上非负
                average_product = sum(x * y for x, y in pairs) / count
                variance_estimator = (count / (count - 1) ** 3) * sum(
                    (x * y - average_product) ** 2 for x, y in pairs)
                pi_sum += variance_estimator
            target_sum += sample[i][j] ** 2
    shrinkage = min(1.0, pi_sum / target_sum) if target_sum > 0 else 1.0
    shrunk = [[(1 - shrinkage) * sample[i][j] if i != j else sample[i][j]
               for j in range(n)] for i in range(n)]
    ridge = max(VARIANCE_FLOOR, RIDGE_SCALE * max(shrunk[i][i] for i in range(n)))
    for i in range(n):
        shrunk[i][i] += ridge
    raw_pd = linalg.is_positive_definite(sample)
    diagnostics = {'shrinkage': shrinkage, 'ridge': ridge,
                   'raw_positive_definite': raw_pd,
                   'short_history_nodes': short,
                   'zero_variance_nodes': sorted(zero_variance),
                   'residual_weeks': weeks}
    return shrunk, diagnostics


def reconcile_week(base, snap, s_matrix, weight_inv):
    """对一个预测周做无约束 MinT 与带约束协调。"""
    ordered = snap['ordered']
    leaves = snap['leaves']
    b = [base[node] for node in ordered]
    st = linalg.transpose(s_matrix)
    sw = linalg.matmul(st, weight_inv)
    q = linalg.matmul(sw, s_matrix)
    c = linalg.matvec(sw, b)
    y_free = linalg.solve(q, c)
    x_min_t = linalg.matvec(s_matrix, y_free)
    fixed = [j for j, leaf in enumerate(leaves) if leaf in set(snap['fixed_zero']) & set(leaves)]
    y_qp, active = linalg.nonnegative_qp(q, c, fixed,
                                         initial=[max(0.0, v) for v in y_free])
    # 数值零处理：仅把 [0, 容差) 的微值归零，不触碰任何负值；
    # 父节点由归零后的叶解经 S 重建，因此守恒仍然精确成立。
    zero_tol = 1e-12 * max([1.0] + [abs(v) for v in y_qp])
    for j, leaf in enumerate(leaves):
        if 0.0 <= y_qp[j] < zero_tol:
            y_qp[j] = 0.0
            if j not in fixed and j not in active['active_nonneg']:
                active['active_nonneg'].append(j)
    x_final = linalg.matvec(s_matrix, y_qp)
    gaps = [x_final[i] - sum(s_matrix[i][j] * y_qp[j] for j in range(len(leaves)))
            for i in range(len(ordered))]
    return {
        'base': dict(zip(ordered, b)),
        'reconciled_unconstrained': dict(zip(ordered, x_min_t)),
        'reconciled': dict(zip(ordered, x_final)),
        'leaf_solution': dict(zip(leaves, y_qp)),
        'constrained': {
            'active_nonneg': [leaves[j] for j in active['active_nonneg']],
            'fixed_zero': [leaves[j] for j in fixed],
            'multipliers': {leaves[j]: value for j, value in active['multipliers'].items()}},
        'conservation': {'max_parent_gap': max(abs(gap) for gap in gaps),
                         'min_value': min(x_final), 'all_nonneg': all(v >= -1e-10 for v in x_final)}}


def _weighted_loss(b, x, weight_inv):
    diff = [bi - xi for bi, xi in zip(b, x)]
    return sum(diff[i] * sum(weight_inv[i][j] * diff[j] for j in range(len(diff)))
               for i in range(len(diff)))


def run_forecast(records, structure, origin, horizon, closures_from_data=True):
    series, audit = rebuild(records, structure, origin)
    snap, s_matrix = structure.summing_matrix(origin)
    closed = set(structure.closed_leaves(origin))
    if closures_from_data:
        for leaf in snap['leaves']:
            entries = series.get(leaf, [])
            if entries and entries[-1].get('closed') == 1:
                closed.add(leaf)
    snap['fixed_zero'] = sorted(closed & set(snap['leaves']))
    fits = {}
    for node in snap['ordered']:
        entries = series.get(node, [])
        fits[node] = base_forecast(entries, horizon)
    w_matrix, cov_diag = error_covariance(snap['ordered'], fits)
    w_inv = linalg.symmetric_inverse(w_matrix)
    if w_inv is None:
        raise RuntimeError('正则化后的误差协方差仍不可逆')
    weeks_out = []
    for step in range(horizon):
        week = origin + step + 1
        base = {node: fits[node]['forecast'][step] for node in snap['ordered']}
        result = reconcile_week(base, snap, s_matrix, w_inv)
        ordered = snap['ordered']
        base_vec = [base[node] for node in ordered]
        mint_vec = [result['reconciled_unconstrained'][node] for node in ordered]
        final_vec = [result['reconciled'][node] for node in ordered]
        loss = {
            'adjustment_minT_weighted_sse': _weighted_loss(base_vec, mint_vec, w_inv),
            'adjustment_constrained_weighted_sse': _weighted_loss(base_vec, final_vec, w_inv),
            'nonnegativity_extra_weighted_sse': _weighted_loss(mint_vec, final_vec, w_inv),
            'adjustment_minT_sse': sum((base[n] - result['reconciled_unconstrained'][n]) ** 2 for n in ordered),
            'adjustment_constrained_sse': sum((base[n] - result['reconciled'][n]) ** 2 for n in ordered)}
        result['week'] = week
        result['loss'] = loss
        weeks_out.append(result)
    return {
        'product': 'hierarchy-reconciliation', 'schema_version': 2, 'unit': '工时',
        'origin': origin, 'horizon': [origin + step + 1 for step in range(horizon)],
        'structure': {
            'root': ROOT, 'nodes': snap['ordered'], 'leaves': snap['leaves'],
            'levels': snap['levels'], 'fixed_zero': snap['fixed_zero'],
            'mapping_events': audit['mapping_events']},
        'base_models': {node: {'kind': fits[node]['kind'],
                               'short_history': fits[node].get('short_history', False)}
                        for node in snap['ordered']},
        'error_covariance': cov_diag,
        'weeks': weeks_out,
        'audit': audit}
