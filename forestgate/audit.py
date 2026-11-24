"""Rebuild current forest access decisions from immutable input bytes."""
import hashlib,tempfile
from contextlib import closing
from pathlib import Path
from .store import connect,report
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .lidar import import_plot,import_return
from .stand_volume import import_datum,import_control,import_stem_plot,import_survey

FIELDS=('blocks','stations','risk','samples','conflicts','shifts','fires','access','rechecks','challenges','challenge_states',
        'lidar_plots','lidar_returns','timber_estimates','flight_datums','ground_controls','stem_plots','stem_trees','stand_assessments')

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
            {'station':import_station,'station_revoke':revoke_station,'risk':import_risk,'sample':import_sample,'shift':import_shift,'patrol':import_patrol,'access':import_access,'recheck':recheck_access,'field_challenge':import_field_challenge,
             'lidar_plot':import_plot,'lidar_return':import_return,'flight_datum':import_datum,'ground_control':import_control,'stem_plot':import_stem_plot,'stand_survey':import_survey}[item['kind']](rebuilt,file)
        replay=report(rebuilt)
    return {'status':'matched' if all(original[k]==replay[k] for k in FIELDS) else 'divergent',
            'source_count':len(sources),'differences':[k for k in FIELDS if original[k]!=replay[k]]}
