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

当前只有离线林班与单机 SQLite，见证人与作业员只是 fixture 身份，不提供真实签名和现场控制。跨林场蔓延、卫星热点、航空巡护及人员撤离均未实现。

## 海盐雾季的苗圃含水边界与阀门计划

电容探针的单频读数在盐雾季服从合成曲线 `C = W + S`（W 为体积含水百分数，S 为盐分贡献）。同一原始读数 C=50 同时相容状态甲（W=20、S=30，偏干，100 m² 需 400 升）与状态乙（W=40、S=10，湿润，需 0 升）；旧 `reading-import` 仍把电容当含水直接与 30 比较，保留为**原采录投影**（P1=50 → 0 升可复现），盐雾季的可行范围、阀门计划、水箱余量与土样证据另存版本化表，不覆盖旧建议。

新增采录均通过同一 `source` 账本保存原始字节与 SHA-256：`calibration-import` 登记校准批次共同满足的盐分偏移区间（同批探针共享一个偏移，禁止逐样方各选有利值）；`probe-import` 登记探针频率、温度、批号与盐分敏感曲线；`tank-import` 登记水箱余量与其服务的相邻样方；`scan-import` 登记第二频 `D=W−S` 扫测，覆盖格解出 `W=(C+D)/2`，被遮挡畦垄显式记为未覆盖；`soil-sample-import` 登记独立采样员对固定 1 m² 格网的烘干称重，证据只约束该格，禁止外推。两条证据路线互斥，同一地块不得静默混用。

`moisture-plan` 先输出每块样方（及每格）的可行 W 区间与最少/最多需水量，再枚举校准批次共享偏移的全部角点联合状态（含每格干/湿翻转点），标明每个状态是否放得下水箱；阀门动作只下发在**所有可行状态都安全**的水量，盐分未决的格标记 `needs_measurement` 而不是猜一个水量。确认缺水总量超过水箱余量时，按确定优先级分配并把未服务样方标记 `awaiting_refill`，计划永不静默超用。计划按 `(plan_id, version)` 版本化，`plan_joint_state/plan_cell/valve_action` 保留每次联合状态与证据来源 id；`audit` 仍可从原始字节重放全部证据投影。

```powershell
python -m forestgate plot-import --db demo.sqlite --file <nursery_plot.json>
python -m forestgate calibration-import --db demo.sqlite --file <calibration_batch.json>
python -m forestgate probe-import --db demo.sqlite --file <probe.json>
python -m forestgate reading-import --db demo.sqlite --file <soil_reading.json>
python -m forestgate tank-import --db demo.sqlite --file <water_tank.json>
python -m forestgate moisture-plan --db demo.sqlite --plan-id saline --tank-id T-600
python -m forestgate scan-import --db demo.sqlite --file <dual_frequency_scan.json>
python -m forestgate moisture-plan --db demo.sqlite --plan-id saline --tank-id T-600
```

合成验收场景见 `fixtures/saline-nursery.json`（含 P1/P2、共享批次 CB-1、600/300 升水箱、双频全区扫描与单格烘干样本）与 `tests/test_moisture_bounds.py`：C=50 的两个世界对应 400/0 升且先保留范围；共享校准下不会出现逐格最优的 400+0 组合；双频全区只收窄已扫的 75 m²/块、遮挡畦垄仍待补测；烘干样本只确认 1 m²（4 升），不外推到其余 99 m² 或 P2。
