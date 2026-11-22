"""Run three cross-ledger paths against a disposable local evidence book."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import forestgate
from forestgate.access import import_access, import_field_challenge
from forestgate.audit import audit
from forestgate.risk import import_risk, import_station
from forestgate.store import report


def main():
    fixtures = Path(__file__).resolve().parents[1] / 'fixtures'
    with tempfile.TemporaryDirectory(prefix='forestgate-demo-') as folder:
        db = Path(folder) / 'demo.sqlite'
        import_station(db, fixtures / 'station-ok.json')
        import_risk(db, fixtures / 'risk-ok.json')
        granted = import_access(db, fixtures / 'access-ok.json')
        first = import_field_challenge(db, fixtures / 'field-a.json')
        second = import_field_challenge(db, fixtures / 'field-b.json')
        held = report(db)['access'][0]
        replay = audit(db)
        assert granted['status'] == 'granted'
        assert first['status'] == 'pending'
        assert second['status'] == 'confirmed_discrepancy'
        assert held['status'] == 'held'
        assert replay['status'] == 'matched'
        print(json.dumps({'module_file': forestgate.__file__, 'grant': granted['status'], 'challenge': second['status'],
                          'after_challenge': held['status'], 'audit': replay}, ensure_ascii=False))


if __name__ == '__main__':
    main()
