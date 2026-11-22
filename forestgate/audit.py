"""Rebuild current forest access decisions from immutable input bytes."""
import hashlib,tempfile
from contextlib import closing
from pathlib import Path
from .store import connect,report
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .wildlife import import_camera,import_frame
from .individuals import import_clock,import_gap,import_media,import_cosighting,publish_assessment

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
            {'station':import_station,'station_revoke':revoke_station,'risk':import_risk,'sample':import_sample,'shift':import_shift,'patrol':import_patrol,'access':import_access,'recheck':recheck_access,'field_challenge':import_field_challenge,'wildlife_camera':import_camera,'wildlife_frame':import_frame,'wildlife_clock':import_clock,'wildlife_gap':import_gap,'wildlife_media':import_media,'wildlife_cosighting':import_cosighting,'wildlife_assessment':publish_assessment}[item['kind']](rebuilt,file)
        replay=report(rebuilt)
    fields=('blocks','stations','risk','samples','conflicts','shifts','fires','access','rechecks','challenges','challenge_states',
            'wildlife_cameras','wildlife_frames','wildlife_counts','wildlife_clocks','wildlife_gaps','wildlife_media','wildlife_cosightings','wildlife_assessments')
    return {'status':'matched' if all(original[k]==replay[k] for k in fields) else 'divergent',
            'source_count':len(sources),'differences':[k for k in fields if original[k]!=replay[k]]}
