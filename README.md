# 林班防火禁入采证台

这是离线的林班级火险、巡护火源和入山许可证据账本。所有数据均为合成 fixture，不连接真实气象站或入山闸机。项目的目标是让火险等级、独立火源见证和作业许可彼此改变业务决定，同时保留原始字节与旧裁定。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m forestgate station-import --db demo.sqlite --file fixtures/station-ok.json
python -m forestgate risk-import --db demo.sqlite --file fixtures/risk-ok.json
python -m forestgate access-import --db demo.sqlite --file fixtures/access-ok.json
python -m forestgate patrol-import --db demo.sqlite --file fixtures/patrol-ok.json
python -m forestgate patrol-import --db demo.sqlite --file fixtures/patrol-close.json
python -m forestgate audit --db demo.sqlite
python -m forestgate report --db demo.sqlite
python -m pytest -q
```

三账有独立命令与持久表：`forest_block/risk_observation` 保存林班与风险版次；`fire_event/fire_state` 保存巡护见证和火源事件；`access_request/access_state` 保存作业申请和当前许可。迟到高火险观测会把原来准许的许可转为暂停；独立火源上报会暂停同林班许可，关闭见证又可恢复；无校准资格的站点使当前风险待核，旧许可随之暂缓。`decision_log` 保留每次变更，`audit` 从原始文件字节重放并比较当前投影。

当前只有离线林班与单机 SQLite，见证人与作业员只是 fixture 身份，不提供真实签名和现场控制。跨林场蔓延、卫星热点、航空巡护及人员撤离均未实现。
