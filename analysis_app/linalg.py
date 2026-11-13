"""纯标准库线性代数与约束二次规划。

项目不允许第三方依赖，这里只实现 forecast 流程需要的小规模矩阵运算：
高斯-若尔当求逆/解方程、Cholesky 判定，以及下界约束（x >= 0、部分 x = 0）
的凸二次规划主动集求解。节点规模在数十量级，可读性优先于极致性能。
"""
import math

TOL = 1e-12


def identity(n):
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def matmul(a, b):
    rows, inner, cols = len(a), len(b), len(b[0])
    out = [[0.0] * cols for _ in range(rows)]
    for i in range(rows):
        ai = a[i]
        oi = out[i]
        for k in range(inner):
            aik = ai[k]
            if aik == 0.0:
                continue
            bk = b[k]
            for j in range(cols):
                oi[j] += aik * bk[j]
    return out


def matvec(a, x):
    return [sum(a[i][j] * x[j] for j in range(len(x))) for i in range(len(a))]


def transpose(a):
    return [list(col) for col in zip(*a)]


def symmetric_inverse(a, ridge=0.0):
    """高斯-若尔当求对称正定矩阵的逆，失败时返回 None。"""
    n = len(a)
    work = [row[:] for row in a]
    if ridge:
        for i in range(n):
            work[i][i] += ridge
    inv = identity(n)
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(work[r][col]))
        if abs(work[pivot][col]) <= TOL:
            return None
        if pivot != col:
            work[col], work[pivot] = work[pivot], work[col]
            inv[col], inv[pivot] = inv[pivot], inv[col]
        divisor = work[col][col]
        work[col] = [v / divisor for v in work[col]]
        inv[col] = [v / divisor for v in inv[col]]
        for r in range(n):
            if r == col:
                continue
            factor = work[r][col]
            if factor == 0.0:
                continue
            work[r] = [v - factor * u for v, u in zip(work[r], work[col])]
            inv[r] = [v - factor * u for v, u in zip(inv[r], inv[col])]
    return inv


def solve(a, b):
    """解 A x = b，A 必须非奇异（带部分选主元的高斯消元）。"""
    n = len(a)
    work = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(work[r][col]))
        if abs(work[pivot][col]) <= TOL:
            raise ValueError('系数矩阵奇异，无法求解')
        if pivot != col:
            work[col], work[pivot] = work[pivot], work[col]
        divisor = work[col][col]
        work[col] = [v / divisor for v in work[col]]
        for r in range(n):
            if r == col:
                continue
            factor = work[r][col]
            if factor:
                work[r] = [v - factor * u for v, u in zip(work[r], work[col])]
    return [work[i][n] for i in range(n)]


def is_positive_definite(a):
    try:
        cholesky(a)
    except ValueError:
        return False
    return True


def cholesky(a):
    n = len(a)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            total = sum(low[i][k] * low[j][k] for k in range(j))
            if i == j:
                value = a[i][i] - total
                if value <= TOL:
                    raise ValueError('矩阵不是正定矩阵')
                low[i][j] = math.sqrt(value)
            else:
                low[i][j] = (a[i][j] - total) / low[j][j]
    return low


def submatrix(a, rows, cols):
    return [[a[i][j] for j in cols] for i in rows]


def nonnegative_qp(q, b, fixed_zero, initial=None):
    """求解 min 0.5 x' Q x - b' x ，约束 x >= 0 且 fixed_zero 中的变量恒为 0。

    使用原始主动集法：等式面内求最优，触碰下界则加入主动集，
    依据 KKT 乘子释放主动约束。fixed_zero 是停用节点的硬约束，
    永不释放；普通非负约束可以按乘子释放，因此这不是事后裁零。
    返回 (x, active_info)，active_info 给出被约束节点与乘子。
    """
    n = len(q)
    fixed = set(fixed_zero)
    if initial is None:
        x = [0.0 if i in fixed else max(0.0, 0.0) for i in range(n)]
    else:
        x = [0.0 if i in fixed else max(0.0, initial[i]) for i in range(n)]
    active = {i for i in range(n) if i in fixed or x[i] == 0.0}
    scale = max(1.0, max(abs(q[i][i]) for i in range(n)))
    tol = 1e-10 * scale
    for _ in range(10 * n + 50):
        free = [i for i in range(n) if i not in active]
        direction = x[:]
        if free:
            qff = submatrix(q, free, free)
            rhs = [b[i] - sum(q[i][j] * x[j] for j in active) for i in free]
            solved = solve(qff, rhs)
            for index, value in zip(free, solved):
                direction[index] = value
        violations = [i for i in free if direction[i] < -tol]
        if violations:
            alpha = 1.0
            blocking = None
            for i in violations:
                step = x[i] / (x[i] - direction[i])
                if step < alpha:
                    alpha, blocking = step, i
            for i in range(n):
                if i in free:
                    x[i] = x[i] + alpha * (direction[i] - x[i])
            active.add(blocking)
            x[blocking] = 0.0
            continue
        x = direction
        gradient = [sum(q[i][j] * x[j] for j in range(n)) - b[i] for i in range(n)]
        releasable = [(gradient[i], i) for i in active if i not in fixed and gradient[i] < -tol]
        if releasable:
            active.remove(min(releasable)[1])
            continue
        multipliers = {i: gradient[i] for i in active if i not in fixed}
        return x, {'active_nonneg': sorted(multipliers), 'multipliers': multipliers,
                   'fixed_zero': sorted(fixed)}
    raise RuntimeError('主动集求解超过迭代上限')
