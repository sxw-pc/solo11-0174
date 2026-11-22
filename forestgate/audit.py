"""Rebuild current forest access decisions from immutable input bytes."""
import json,tempfile
from contextlib import closing
from pathlib import Path
from .store import connect,report
from .risk import import_risk,import_station,revoke_station
from .patrol import import_patrol
from .access import import_access,recheck_access

def audit(db_path):
    with closing(connect(db_path)) as db:
        sources=[dict(row) for row in db.execute('SELECT id,kind,sha,raw FROM source ORDER BY imported_seq')]
    original=report(db_path)
    with tempfile.TemporaryDirectory(prefix='forestgate-audit-') as folder:
        rebuilt=Path(folder)/'replay.sqlite'
        for i,item in enumerate(sources):
            file=Path(folder)/f'{i}.json';file.write_bytes(item['raw'])
            {'station':import_station,'station_revoke':revoke_station,'risk':import_risk,'patrol':import_patrol,'access':import_access,'recheck':recheck_access}[item['kind']](rebuilt,file)
        replay=report(rebuilt)
    fields=('blocks','stations','risk','fires','access','rechecks')
    return {'status':'matched' if all(original[k]==replay[k] for k in fields) else 'divergent',
            'source_count':len(sources),'differences':[k for k in fields if original[k]!=replay[k]]}
