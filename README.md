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

## 倾斜临时支脚油罐的实油证据

`fuel_depot` 的 `stock_l` 只是开库声明值，临时支脚倾斜时不能替代实测可抽取量。`forestgate/dip_volume.py` 用罐身坐标积分：罐首 x=0 油尺 h、底坡 s（s>0 为罐底向远端降低），液深 d(x)=clamp(h+s·x, 0, H)，按分段梯形解析积分得到总油量，吸入口平面以下的死油楔只从可抽取量扣除，测量误差在端点上求值给出上下界。2m×1m×0.6m 长方罐、h=0.3m 时，s=+0.1 与 s=−0.1 分别为 800 升与 400 升，水平误算恒为假单值 600 升；单凭一根油尺只能得到坡度批准带内的区间（400–800 升），禁止取最有利端点写实库存。

声明账与实油账分开且互不覆盖：`tank_geometry/tank_dip/tank_tilt/level_transfer` 保存几何、单点油尺、支脚倾角和调平流量计原件，`dip_corroboration` 按版次记录每次尺读的确证区间，`fuel_mission_revision` 给已承诺任务开编号版次。规则：

- 同一支脚 epoch、同一时刻的两端倾角记录才能与油尺匹配；晚导入的旧倾角只增加该时刻的确证版次，不能倒用到其他 epoch（支脚移动后的读数）。
- 倾角证明 +0.1 时历史 FM1 800 升承诺开 `physical_verified`；证明 −0.1 时原 `reserved` 保留，另开 `physical_shortage`、缺口 400 升，触发异站调拨。
- 调平后流量计只给调平时刻之后的新任务背书；`transfer_predates_commitment` 拒绝为历史 FM1 倒签，且同一批实测油要扣除已拨给其他任务的需求，不重复承诺。
- 单尺无倾角时任务只能是 `physical_unverified`，绝不允许凭一个读数标成实油保证。

`fixtures/tilted-depot.json` 是一份含声明、几何、油尺与任务的批量原件；`tests/test_dip_volume.py` 覆盖升到升的双世界反例、死油与误差界、版次规则、两路补测的不同任务集合及审计重放。

## 离线测试

标准环境直接 `python -m pytest -q`。本沙箱镜像没有 pip/pytest，仓库内置标准库垫片 `.pyshim/`（实现 tmp_path、pytest.raises、pytest.approx 的最小子集），运行：

```shell
PYTHONPATH=.pyshim python3 -m pytest -q
```

该垫片只在显式设置 PYTHONPATH 时生效，安装了真实 pytest 的环境不受影响。

