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
