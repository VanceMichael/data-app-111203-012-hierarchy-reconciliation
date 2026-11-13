# 数据契约

设备服务网络按区域与站点汇总检修工时和班组数，记录统计时所使用的组织结构版本。

主测量 `hours` 的单位为工时，主体字段是 `depot`，核算维度是 `region`。`observation_id` 是文件内唯一观测编号，主体允许重复观测。数值只接受有限值；空单元格解析为 None，不能直接当成零。额外列不进入当前核算。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `observation_id` | str | 必填 |
| `depot` | str | 必填 |
| `region` | str | 必填 |
| `week` | int | 必填 |
| `hours` | float | 必填 |
| `crew` | int | 必填 |
| `closed` | int | 必填 |
| `structure_version` | str | 必填 |

时间字段 day、week、tick、hour、minute、cycle、start_day、reported_day、known_at、visible_at、opened、observed 均以各字段名所述的相对时间单位表示整数；不同字段并不必然代表同一种时间。缺少的登记码与浓度等信息保持为空。示例只表达产品数据结构，不能代替实际调查资料。

## 业务字段语义

hours 为检修工时，crew 为班组数，closed 为停用标志。structure_version 是记录对应的组织版本，week 为观察周。region 不保证跨时间恒定，汇总以当前行标记为准。

## 本地结构配置（forecast 流程）

组织结构来自项目数据目录中的三份本地 CSV，不接组织管理平台。预测只读取这些文件与观测文件，全部处理在本地完成。

### `data/structure.csv`

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `node` | str | 必填，节点名；根节点固定为 `NAT` |
| `parent` | str | 必填，父节点；根节点不能有父节点 |
| `effective_week` | int | 必填，非负，自该周起边生效 |

同一节点在同一周只能有一个父节点，重复挂接（同节点同父同周的重复边或同周两个父节点）拒绝；任意周快照不得成环或自环；所有节点必须沿父链连到 `NAT`。节点在新生效周换父即表达拆区/跨区调整，历史周仍按当周生效边归属（as-was）。

### `data/closures.csv`

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `node` | str | 必填，必须在结构中声明 |
| `closed_week` | int | 必填，自该周起站点停用 |

停用节点在预测期被钉为零工时（QP 的固定零约束，不可释放），不是先预测再裁零。

### `data/node_mappings.csv`

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `source_node` | str | 必填 |
| `target_node` | str | 必填 |
| `effective_week` | int | 必填，非负 |
| `strategy` | str | 必填，`as_was` / `merge_series` / `drop_after_merge` 之一 |

并站用 `merge_series`：生效周起源节点移出在册叶节点集合，其生效周之前的建模序列并入目标节点序列，每次并入都在输出的 `merge_contributions` 留痕；历史周的区域/全国汇总仍按当周结构计算，全国总量不重复计算。`drop_after_merge` 仅移除源节点，不改写任何序列。任何策略都不得用未来归属回填历史区域绩效。
