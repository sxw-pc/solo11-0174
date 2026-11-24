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

## 采伐前蓄积区间（lidar / stand_volume）

第四账处理无人机激光扫描的采伐前蓄积。`plot-import`/`return-import` 只保存样方面积与原始回波（beam、z、回波序号），回波顺序不做地面分类——坡面或低矮灌木可能占末回波。`timber.py` 保留旧版 top-return 单值估计（最高点减零米×面积×0.02）作为已交付采伐依据，后续控制点不得改写。`datum-import` 登记整次飞行共享的标高基准（地面下限与唯一偏置先验）；`control-import` 的 RTK 控制点联立约束这一个共享偏置并钉住本样方地面，互相矛盾的偏置或压在回波之下的地面会被拒绝；`stem-plot-import` 逐株登记胸径与树高，只确证定面积样地小格的实材积与树种，面积不得外推。`stand` 命令发布每个样方可证明的地面/冠层组合与蓄积区间：林班合计只由完整场景（一个共享偏置加每样方地面）取到界限并附见证场景，无冠层回波的漏扫带保持未测而不是零蓄积。`survey-import` 可一次导入 `fixtures/occluded-stand.json` 这样的整包调查，子文档各自登记哈希源；`audit` 重放覆盖全部新表。

```powershell
python -m forestgate survey-import --db demo.sqlite --file fixtures/occluded-stand.json
python -m forestgate stand --db demo.sqlite --block B1
```

当前只有离线林班与单机 SQLite，见证人与作业员只是 fixture 身份，不提供真实签名和现场控制。跨林场蔓延、卫星热点、航空巡护及人员撤离均未实现。
