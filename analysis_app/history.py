"""按预测起点可见的组织结构重建各节点历史。

核心口径是 as-was：第 w 周的区域/全国值只按第 w 周生效的父子边汇总，
结构调整前的历史绩效保留在原区域，绝不按当前归属回填。合并映射
（merge_series）只影响目标节点自身的建模序列，并在审计记录中留痕，
不会让任何一周的全国总量被重复计算。
"""
from collections import defaultdict


class ObservationStore:
    def __init__(self, records):
        self._hours = {}
        self._crew = {}
        self._closed = {}
        self.depots = set()
        self.weeks = set()
        for row in records:
            key = (row['depot'], row['week'])
            if key in self._hours:
                raise ValueError(f"站点 {row['depot']} 第 {row['week']} 周存在重复观测")
            if row['hours'] < 0:
                raise ValueError(f"工时不能为负：{row['depot']} week={row['week']}")
            self._hours[key] = row['hours']
            self._crew[key] = row['crew']
            self._closed[key] = row['closed']
            self.depots.add(row['depot'])
            self.weeks.add(row['week'])

    def hours(self, depot, week):
        return self._hours.get((depot, week))

    def crew(self, depot, week):
        return self._crew.get((depot, week))

    def closed(self, depot, week):
        return self._closed.get((depot, week), 0)


def rebuild(records, structure, origin):
    """返回 (series, audit)。

    series[node] 是 week=0..origin 的逐周字典（hours 可能为 None，
    表示该周无观测；closed 取该周在册叶节点的停用标志或）。
    audit 记录每周使用的结构版本事件与合并贡献，保证可追溯。
    """
    store = ObservationStore(records)
    max_week = max(store.weeks) if store.weeks else origin
    end = min(origin, max_week)
    series = defaultdict(list)
    merge_contributions = []
    for week in range(end + 1):
        snap, _ = structure.summing_matrix(week)
        leaf_values = {}
        for leaf in snap['leaves']:
            value = store.hours(leaf, week)
            leaf_values[leaf] = value
            entry = {'week': week, 'hours': value,
                     'crew': store.crew(leaf, week),
                     'closed': max(store.closed(leaf, week),
                                   1 if leaf in structure.closed_leaves(week) else 0)}
            series[leaf].append(entry)
        for node in snap['ordered']:
            if node in snap['leaves']:
                continue
            parts = [leaf_values[leaf] for leaf in snap['leaves']
                     if _is_descendant(snap['parent_of'], leaf, node)]
            values = [value for value in parts if value is not None]
            closed = 1 if node in structure.closed_leaves(week) else 0
            series[node].append({'week': week,
                                 'hours': sum(values) if values else None,
                                 'crew': sum(store.crew(leaf, week) or 0
                                             for leaf in snap['leaves']
                                             if _is_descendant(snap['parent_of'], leaf, node)),
                                 'closed': closed,
                                 'members': [leaf for leaf in snap['leaves']
                                             if _is_descendant(snap['parent_of'], leaf, node)]})
    for item in structure.mappings:
        if item['strategy'] != 'merge_series':
            continue
        source, target, effective = item['source_node'], item['target_node'], item['effective_week']
        for entry in series.get(source, []):
            if entry['week'] >= effective or entry['hours'] is None:
                continue
            target_entry = next((e for e in series[target] if e['week'] == entry['week']), None)
            if target_entry is None:
                continue
            base = target_entry['hours'] or 0.0
            target_entry['hours'] = base + entry['hours']
            target_entry.setdefault('merged_from', []).append(
                {'week': entry['week'], 'source': source, 'hours': entry['hours']})
            merge_contributions.append({'target': target, 'source': source,
                                        'week': entry['week'], 'hours': entry['hours']})
    audit = {'origin': origin, 'history_end': end,
             'mapping_events': structure.mapping_events(),
             'merge_contributions': merge_contributions,
             'rule': 'as-was：历史周按当周生效结构归属，禁止未来归属回填'}
    return dict(series), audit


def _is_descendant(parent_of, leaf, node):
    current = leaf
    while current != node:
        if current not in parent_of:
            return False
        current = parent_of[current]
    return True
