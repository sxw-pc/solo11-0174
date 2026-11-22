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

## 鹿群相机陷阱个体数

`wildlife-camera-import`/`wildlife-frame-import` 登记相机林班、坐标、覆盖半径与帧（物种、局部 track_hint、时刻、质量、开机段），并按 `camera_id:track_hint` 去重形成临时计数 `wildlife_count`；track_hint 只在单机内部有意义，该计数只是原采录投影。`wildlife-clock-import` 记录整段开机共用的时钟偏差区间（同一开机段所有帧共用一个偏移，不能逐张调钟）；`wildlife-gap-import` 记录断档窗口，落在断档内的帧被拒绝；`wildlife-media-import` 把归档 RAW 原图/视频的 SHA-256、归档位与可辨外观标记（耳缺口、鹿角）挂到原帧，来源时刻不闭合的标记不生效；`wildlife-cosighting-import` 记录下轮双机同步重叠视场建立的跨机同现约束，只约束列出的新帧，不倒填旧季。

`wildlife-assessment-publish` 在给定相机范围、季度与最大移动速度下，先把多帧归一为局部轨迹，再枚举候选同一性：合并必须同时满足共用时钟偏差下的速度可达、不可同时出现在两处、外观标记不冲突，且合并关系全局传递一致，不做逐对贪心。证据不足时按 （林班， 物种， 季度） 追加版本发布 [下界， 上界] 及各自对应的完整可行个体分配；补录证据只产生新版本，旧季报与临时计数从不改写。`fixtures/deer-cross-camera.json` 是文档数组（逐条经对应命令导入），复现 C1(0,0) 08:00/08:20 与 C2(2,0) 08:10 的同龄无标记鹿：同一组观测同时支持 1 只（往返）与 2 只（留守+经过）两个世界；另放 C3(50,0) 同时刻远端帧可检验全局合并不会拼出瞬移鹿。`tests/test_individuals.py` 覆盖两条合法补证路径：归档 RAW 耳缺口把旧季上界缩到 1（只覆盖 C1 时 C2 仍留范围），或历史 RAW 损坏后下轮双机同步只约束新季同现个体。

当前只有离线林班与单机 SQLite，见证人与作业员只是 fixture 身份，不提供真实签名和现场控制。跨林场蔓延、卫星热点、航空巡护及人员撤离均未实现。
