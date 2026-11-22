"""Rebuild current forest access decisions from immutable input bytes."""
import hashlib,tempfile
from contextlib import closing
from pathlib import Path
from .store import connect,report
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .fuel import register_depot, import_geometry, import_dip, import_tilt, import_level_transfer, import_evidence_batch
from .mission import reserve_mission, corroborate_mission

def audit(db_path):
    with closing(connect(db_path)) as db:
        sources=[dict(row) for row in db.execute('SELECT id,kind,sha,raw FROM source ORDER BY imported_seq')]
    corrupt=[x['id'] for x in sources if hashlib.sha256(x['raw']).hexdigest()!=x['sha']]
    if corrupt:return {'status':'source_corrupt','source_count':len(sources),'differences':corrupt}
    original=report(db_path)
    with tempfile.TemporaryDirectory(prefix='forestgate-audit-') as folder:
        rebuilt=Path(folder)/'replay.sqlite'
        for i,item in enumerate(sources):
            file=Path(folder)/f'{i}.json';file.write_bytes(item['raw'])
            {'station':import_station,'station_revoke':revoke_station,'risk':import_risk,'sample':import_sample,'shift':import_shift,'patrol':import_patrol,'access':import_access,'recheck':recheck_access,'field_challenge':import_field_challenge,'fuel_depot':register_depot,'fuel_mission':reserve_mission,'tank_geometry':import_geometry,'tank_dip':import_dip,'tank_tilt':import_tilt,'level_transfer':import_level_transfer,'evidence_batch':import_evidence_batch,'mission_corroboration':corroborate_mission}[item['kind']](rebuilt,file)
        replay=report(rebuilt)
    fields=('blocks','stations','risk','samples','conflicts','shifts','fires','access','rechecks','challenges','challenge_states','fuel_depots','fuel_missions','tank_geometry','tank_dips','tank_tilts','level_transfers','dip_corroborations','fuel_mission_revisions')
    return {'status':'matched' if all(original[k]==replay[k] for k in fields) else 'divergent',
            'source_count':len(sources),'differences':[k for k in fields if original[k]!=replay[k]]}
