# 林班防火禁入采证台

这是离线的林班级火险、巡护火源和入山许可证据账本。所有数据均为合成 fixture，不连接真实气象站或入山闸机。项目的目标是让火险等级、独立火源见证和作业许可彼此改变业务决定，同时保留原始字节与旧裁定。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m forestgate station-import --db demo.sqlite --file fixtures/station-ok.json
python -m forestgate risk-import --db demo.sqlite --file fixtures/risk-ok.json
python -m forestgate access-import --db demo.sqlite --file fixtures/access-ok.json
python -m forestgate shift-import --db demo.sqlite --file fixtures/shift-guard-a.json
python -m forestgate shift-import --db demo.sqlite --file fixtures/shift-guard-b.json
python -m forestgate shift-import --db demo.sqlite --file fixtures/shift-guard-c.json
python -m forestgate shift-import --db demo.sqlite --file fixtures/shift-guard-d.json
python -m forestgate patrol-import --db demo.sqlite --file fixtures/patrol-ok.json
python -m forestgate patrol-import --db demo.sqlite --file fixtures/patrol-close.json
python -m forestgate audit --db demo.sqlite
python -m forestgate report --db demo.sqlite
python -m pytest -q
python scripts/demo.py
```

三账有独立命令与持久表：`forest_block/risk_observation/risk_sample/risk_conflict/station_cert` 保存林班、风险版次、双人取样、同版次冲突和站点资格；`patrol_shift/fire_event/fire_state` 保存巡护班次、见证和火源事件及受影响路线段；`access_request/access_recheck/access_state` 保存作业申请、降险后复核和当前许可。高火险观测把原来准许的许可转为暂停；独立火源上报只暂停同林班受影响路线的许可，异班次双人见证关闭后才能恢复。当前风险所用站点证书被撤销时，许可转为待核；撤销未用证书不会改写当前结论。

`risk-import` 可以登记 `split_samples=true` 的待取样观测；两份不同地块和人员的 `sample-import` 才能形成可信等级。相同版次互相矛盾的观测写入 `risk_conflict`，冻结当前结论；新版次可信观测才能解冻。高险降为低险时，已有许可保持待核，须由申请人以外的人员在新版观测之后执行 `access-recheck`。许可侧的 `field-challenge` 允许两名非申请人的不同样方观察员复测路线；若双人证据和测站等级不符，风险被标为待核，须有新版测站观测才能重新定级。所有原始 JSON 字节和 SHA-256、每次裁定均保存在 SQLite；`audit` 检查字节摘要，从导入顺序重放，并比较各业务投影。

可单独复现边界：先按上方前三条命令得到准许，再执行 `python -m forestgate risk-import --db demo.sqlite --file fixtures/risk-conflict.json`，许可转待核；在另一份新数据库中，先导入 `station-east.json`，再导入 `risk-split.json` 与 `sample-a.json`、`sample-b.json`，可看到取样前后从待核到准许的转变。无班次或同组见证、过早复核、失效站点证书、冲突版次都不能静默形成准许。

另一条跨账复现：在仅导入 `station-ok.json`、`risk-ok.json`、`access-ok.json` 的新库中，顺序执行 `field-challenge --file fixtures/field-a.json` 和 `field-challenge --file fixtures/field-b.json`。第一次为待第二份证据，第二次冻结风险及许可；`report` 可查看两个现场样本、异议状态和历史裁定。

## 混合像元可燃物解混（可行区间，不给虚假精确百分比）

旧的三波段单材分类保留为原始采录结果（`signature-import`/`fuel-scan`，最近绝对距离 + material/distance），新分析不覆写它。真实像元同时含松针、草、湿土和遮阴，多个组成可解释同一观测，因此 `forestgate/unmix.py` 不返回点估计，而是对整条样带建立**一个联合线性规划**（`forestgate/lp.py`，纯标准库、精确有理数两阶段单纯形），给出可证明的上下界：

```powershell
python -m forestgate mixture-import  --db demo.sqlite --file fixtures/fuel-mixture.json
python -m forestgate band4-import    --db demo.sqlite --file fixtures/fuel-band4.json
python -m forestgate clipping-import --db demo.sqlite --file fixtures/fuel-clipping.json
python -m forestgate audit --db demo.sqlite
```

三条不可合并的结构性事实被显式建模而非隐藏：

- **材料不可辨识**：当某参考光谱落在其他参考的凸包内（fixture 中湿叶 `[5.5,4,3.5]` 恰好是松针 `[4,6,2]` 与草 `[7,2,5]` 各半），100% 湿叶与松针/草各半都满足零残差，却对应不同干燃料质量。逐点 NNLS 会把待核区间压成一个假点；区间优化保留完整可行域，并在 `non_identifiable_signatures` 点名依赖关系。
- **共享校准偏差**：整条样带共用一次仪器校准，模型对每个波段只有一个偏差变量并在所有样方上联合约束，样方不能各自挑选有利校正；`calibration_bias_interval` 是同一份一致偏差的界。
- **覆盖范围分离**：地面割取只证明被割格子内的干重，`coverage` 把连续覆盖面积、已确证干重、已确证面积和待复采格子分开报告，已测干重绝不外推到未采样区域或整片林班；`block_load_conservative` 给林班负荷保守界，未采样面积在没有物理密度上限时不臆造上界。

经规程批准的两条追加采样路线从同一三波段观测起步、互不混用，各自改变不同部分：第四波段（`fuel-band4.json`）在非遮阴格子取得连续混合比例并缩小区间，但重遮阴格仍留区间、留在复采清单，且光学证据永不替代干重；双人定面积实物割取（`fuel-clipping.json`，两队称重超差即拒绝）只在三个代表格形成高可信干重，未覆盖格仍待估。两条路线的可报告连续覆盖面积、已确证干重和待复采格子均不同。原始来源字节、三阶段分析与旧单材分类都在 SQLite 中可追溯，`audit` 重放并逐投影比对。

当前只有离线林班与单机 SQLite，见证人与作业员只是 fixture 身份，不提供真实签名和现场控制。跨林场蔓延、卫星热点、航空巡护及人员撤离均未实现。
