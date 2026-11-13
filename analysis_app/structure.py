"""带生效周的本地组织结构配置。

配置是项目数据目录中的普通 CSV，不接任何组织管理平台。每条边
``node,parent,effective_week`` 表示从该周起 node 挂在 parent 之下，
直到出现同 node 的下一条边。校验规则：

* 同一周内一个节点只能有一个父节点（重复挂接拒绝）；
* 任意周快照不得成环（环拒绝）；
* 父节点必须已在同一周或更早的配置中声明；
* ``closed_week`` 声明站点停用周；合并/拆分区通过边的生效周表达。

结构变更是显式、可追溯的：``mapping_events`` 列出每次归属变化，
history 重建只能按当周生效的边归属（as-was），禁止用未来归属改写历史。
"""
import csv
from pathlib import Path

ROOT = 'NAT'

STRUCTURE_FIELDS = ['node', 'parent', 'effective_week']
CLOSURE_FIELDS = ['node', 'closed_week']
MAPPING_FIELDS = ['source_node', 'target_node', 'effective_week', 'strategy']
KNOWN_STRATEGIES = {'as_was', 'merge_series', 'drop_after_merge'}


def _read_rows(path, expected):
    with Path(path).open(encoding='utf-8', newline='') as handle:
        reader = csv.DictReader(handle)
        missing = set(expected) - set(reader.fieldnames or [])
        if missing:
            raise ValueError('配置缺少字段：' + ','.join(sorted(missing)))
        rows = []
        for position, raw in enumerate(reader, 2):
            if not any((value or '').strip() for value in raw.values()):
                continue
            row = {}
            for name in expected:
                cell = (raw.get(name) or '').strip()
                if name.endswith('week'):
                    try:
                        row[name] = int(cell)
                    except ValueError:
                        raise ValueError(f'第{position}行周字段不是整数：{name}') from None
                else:
                    row[name] = cell
                if row[name] in ('', None):
                    raise ValueError(f'第{position}行必填字段为空：{name}')
            rows.append(row)
    return rows


class Structure:
    def __init__(self, edges, closures, mappings):
        self.edges = edges
        self.closures = closures
        self.mappings = mappings
        self.weeks = sorted({edge['effective_week'] for edge in edges})
        self.nodes = sorted({edge['node'] for edge in edges}
                            | {edge['parent'] for edge in edges}
                            | {item['node'] for item in closures}
                            | {item['source_node'] for item in mappings}
                            | {item['target_node'] for item in mappings})
        if ROOT not in self.nodes:
            raise ValueError('结构中缺少根节点 ' + ROOT)
        self._validate()

    def _validate(self):
        seen_pairs = set()
        for edge in self.edges:
            pair = (edge['node'], edge['parent'], edge['effective_week'])
            if pair in seen_pairs:
                raise ValueError(f"重复挂接：{edge['node']} -> {edge['parent']} @ {edge['effective_week']}")
            seen_pairs.add(pair)
            if edge['node'] == ROOT:
                raise ValueError('根节点不能有父节点')
            if edge['node'] == edge['parent']:
                raise ValueError(f"自环拒绝：{edge['node']} @ {edge['effective_week']}")
            if edge['effective_week'] < 0:
                raise ValueError('生效周不能为负')
        for item in self.mappings:
            if item['strategy'] not in KNOWN_STRATEGIES:
                raise ValueError(f"未知映射策略：{item['strategy']}")
            if item['effective_week'] < 0:
                raise ValueError('映射生效周不能为负')
        for week in self.weeks:
            parent_of = self.parent_map(week)
            for node, parent in parent_of.items():
                if node == parent:
                    raise ValueError(f'自环拒绝：{node} @ {week}')
            for node in parent_of:
                visited = set()
                current = node
                while current in parent_of:
                    if current in visited:
                        chain = ' -> '.join(sorted(visited))
                        raise ValueError(f'结构成环拒绝（week={week}）：{chain}')
                    visited.add(current)
                    current = parent_of[current]
                if current != ROOT:
                    raise ValueError(f'week={week} 节点 {node} 的祖先 {current} 未连到根 {ROOT}')
        closed_nodes = {item['node'] for item in self.closures}
        unknown = closed_nodes - set(self.nodes)
        if unknown:
            raise ValueError('停用节点未在结构中声明：' + ','.join(sorted(unknown)))

    def parent_map(self, week):
        """返回该周生效的 node -> parent（同一节点取不晚于 week 的最新边）。"""
        latest = {}
        for edge in sorted(self.edges, key=lambda e: e['effective_week']):
            if edge['effective_week'] <= week and (
                    edge['node'] not in latest or latest[edge['node']][0] <= edge['effective_week']):
                if edge['node'] in latest and latest[edge['node']][0] == edge['effective_week']:
                    raise ValueError(
                        f"week={edge['effective_week']} 节点 {edge['node']} 同时存在两个父节点："
                        f"{latest[edge['node']][1]} 与 {edge['parent']}")
                latest[edge['node']] = (edge['effective_week'], edge['parent'])
        return {node: parent for node, (_, parent) in latest.items()}

    def snapshot(self, week):
        parent_of = self.parent_map(week)
        children = {}
        for node, parent in parent_of.items():
            children.setdefault(parent, []).append(node)
        for value in children.values():
            value.sort()
        levels = self._levels(parent_of)
        leaves = sorted(node for node in self.nodes
                        if node in parent_of and node not in children
                        and node not in self.merged_away_at(week))
        away = self.merged_away_at(week)
        ordered = [node for node in self._ordered(parent_of, children, leaves)
                   if node not in away]
        return {'week': week, 'parent_of': parent_of, 'children': children,
                'levels': levels, 'leaves': leaves, 'ordered': ordered,
                'merged_away': sorted(away)}

    def merged_away_at(self, week):
        """merge_series/drop_after_merge 映射在生效周后把源节点移出叶节点集合。"""
        away = set()
        for item in self.mappings:
            if item['effective_week'] <= week and item['strategy'] in {'merge_series', 'drop_after_merge'}:
                away.add(item['source_node'])
        return away

    def closed_leaves(self, week):
        return sorted(item['node'] for item in self.closures
                      if item['closed_week'] <= week and item['node'] in self.nodes)

    def _levels(self, parent_of):
        levels = {ROOT: 0}

        def depth(node):
            if node in levels:
                return levels[node]
            parent = parent_of[node]
            levels[node] = depth(parent) + 1
            return levels[node]

        for node in parent_of:
            depth(node)
        return levels

    def _ordered(self, parent_of, children, leaves):
        ordered = [ROOT]
        stack = list(reversed(children.get(ROOT, [])))
        while stack:
            node = stack.pop()
            ordered.append(node)
            stack.extend(reversed(children.get(node, [])))
        return ordered

    def mapping_events(self):
        """归属变化清单，作为结构变更的可追溯审计记录。"""
        events = []
        previous = {}
        for week in self.weeks:
            current = self.parent_map(week)
            for node in sorted(current):
                if node in previous and previous[node] != current[node]:
                    events.append({'node': node, 'week': week,
                                   'from_parent': previous[node], 'to_parent': current[node],
                                   'strategy': 'as_was'})
            previous = current
        for item in self.mappings:
            events.append({'node': item['source_node'], 'week': item['effective_week'],
                           'from_parent': item['source_node'], 'to_parent': item['target_node'],
                           'strategy': item['strategy']})
        return sorted(events, key=lambda e: (e['week'], e['node']))

    def summing_matrix(self, week):
        """构建该周的求和矩阵 S：行=全部节点（根在前），列=在册叶节点。"""
        snap = self.snapshot(week)
        leaves = snap['leaves']
        leaf_index = {leaf: j for j, leaf in enumerate(leaves)}
        rows = []
        for node in snap['ordered']:
            row = [0.0] * len(leaves)
            for leaf in leaves:
                current = leaf
                while current != node and current in snap['parent_of']:
                    current = snap['parent_of'][current]
                if current == node:
                    row[leaf_index[leaf]] = 1.0
            rows.append(row)
        return snap, rows


def load_structure(structure_path, closure_path=None, mapping_path=None):
    edges = _read_rows(structure_path, STRUCTURE_FIELDS)
    closures = _read_rows(closure_path, CLOSURE_FIELDS) if closure_path and Path(closure_path).exists() else []
    mappings = _read_rows(mapping_path, MAPPING_FIELDS) if mapping_path and Path(mapping_path).exists() else []
    return Structure(edges, closures, mappings)
