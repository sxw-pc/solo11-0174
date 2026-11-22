from forestgate.fuel import register_depot
from forestgate.mission import reserve_mission
from forestgate.store import report
from forestgate.audit import audit

def test_fuel_reservation_baseline(tmp_path):
    db = tmp_path / 'fuel.sqlite'
    assert register_depot(db, 'fixtures/fuel-depot-ok.json')['stock_l'] == 1000
    assert reserve_mission(db, 'fixtures/fuel-mission-ok.json')['status'] == 'reserved'
    assert report(db)['fuel_depots'][0]['stock_l'] == 200
    assert audit(db)['status'] == 'matched'
