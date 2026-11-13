# hierarchy-reconciliation

设备服务网络按区域与站点汇总检修工时和班组数，记录统计时所使用的组织结构版本。
forecast 流程在同一套带生效周的本地结构上生成全国/区域/站点三级基准预测，
并协调成层级守恒（父=子之和）、全部非负的预测结果，供排班直接读取。

## 使用

运行需要 Python 3.11 或更新版本，没有第三方运行依赖。数据在本地处理，不请求外部接口。

```bash
python3 -m analysis_app summary
python3 -m analysis_app summary --input data/observations.csv --output output/summary.json
python3 -m analysis_app forecast --origin 8 --horizon 4 --output output/forecast.json
python3 -m analysis_app demo --validate --output output/schedule.json
```

标准输出是完整 JSON，输出文件使用临时文件原子替换；失败退出码为 2，不覆盖既有结果。空数据集保留零计数与空分组，缺失数值不进入数值分母。数据指纹由按 observation_id 排序后的解析记录计算。

## forecast 流程口径

1. **结构校验与快照**：`data/structure.csv` 的每条边带 `effective_week`，同一节点同一周只能有一个父节点；环、自环、重复挂接、未连到根 `NAT` 的配置一律拒绝。
2. **as-was 历史重建**：每个预测起点只用当时可见的边重建各节点序列，第 w 周的区域值只按第 w 周生效的归属汇总；结构变更在 `mapping_events` 中可追溯，禁止用未来归属改写历史区域绩效。并站（`merge_series`）只并入建模序列并留痕，不重复计算全国总量。
3. **各层独立基准预测**：全国、区域、站点各自在重建序列上建模（阻尼 Holt；短历史退化为扩展均值；全零历史预测零并加方差下界；停用节点预测零），层间不共享结果，可独立比较精度。
4. **仅用训练窗误差的协调**：用滚动一步残差估计误差协方差，Schäfer–Strimmer 收缩加岭处理奇异/病态矩阵与短历史节点；先解无约束 MinT，再在同一二次目标上做带非负与停用零约束的主动集 QP。非负性是优化器内部约束，事后裁零无法同时满足守恒，代码不采用。
5. **输出**：每周给出基准值、无约束协调值、最终协调值、相对基准的加权/不加权损失变化、非负约束生效节点与停用零值节点、守恒与非负检查。
6. **滚动验证**：逐起点只用当时可见数据，同时报告站点误差（MAE/RMSE/偏差）与区域资源偏差，并在 `tradeoff` 中显式给出两者取舍。

`demo` 会打印守恒检查、协调前后对比、损失变化、结构变更追溯与滚动验证，并把排班可读 JSON（按周给全部节点的协调工时与叶节点解）写到 `--output`。

## 验证与容器

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q analysis_app
docker build -t hierarchy-reconciliation .
docker run --rm hierarchy-reconciliation
```

容器内也可执行 `python -m unittest discover -s tests -v`。镜像构建阶段取得 Python 镜像，产品启动、测试和演示均无需下载。小规模样例的完整单元测试和 summary 可以连续执行，不需要等待定时任务。

## 代码与数据

- `analysis_app/io.py`：有类型的 CSV 读取、完整性校验及内容指纹。
- `analysis_app/schema.py`：字段类型、主体与聚合维度。
- `analysis_app/domain.py`：当前业务核算函数。
- `analysis_app/summary.py`：确定性的分组与数值口径。
- `analysis_app/structure.py`：带生效周的结构配置、校验、周快照与求和矩阵。
- `analysis_app/history.py`：预测起点可见结构下的 as-was 历史重建与合并留痕。
- `analysis_app/linalg.py`：纯标准库矩阵运算与非负约束主动集二次规划。
- `analysis_app/forecast.py`：分层基准预测、误差协方差收缩与 MinT/约束协调。
- `analysis_app/validation.py`：滚动原点验证与站点/区域口径取舍。
- `analysis_app/cli.py`：本地命令及原子输出。
- `docs/data.md`：数据契约与结构配置说明。

演示数据全部为本地合成，不对应现实机构、设备或人员；不能用来推断真实业务效果。
