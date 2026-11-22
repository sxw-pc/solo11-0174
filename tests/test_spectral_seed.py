from forestgate.spectra import import_signature
from forestgate.scan import import_scan
from forestgate.store import report
from forestgate.audit import audit

def test_single_material_baseline(tmp_path):
    db = tmp_path / 'spectral.sqlite'
    import_signature(db, 'fixtures/signature-pine.json')
    import_signature(db, 'fixtures/signature-grass.json')
    assert import_scan(db, 'fixtures/fuel-scan-ok.json')['material'] == 'pine_litter'
    assert report(db)['fuel_scans'][0]['distance'] == 1
    assert audit(db)['status'] == 'matched'
