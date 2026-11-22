import json
from forestgate.hotspot import import_tile
from forestgate.store import report
from forestgate.audit import audit

def test_tile_capture_baseline(tmp_path):
    db = tmp_path / 'tiles.sqlite'
    fixture = 'fixtures/tile-east.json'
    assert import_tile(db, fixture)['status'] == 'captured'
    assert import_tile(db, fixture)['replayed']
    assert report(db)['tiles'][0]['hot_pixels'] == 5
    assert audit(db)['status'] == 'matched'
