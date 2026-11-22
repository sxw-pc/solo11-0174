"""Rebuild current forest access decisions from immutable input bytes.

Mosaic outputs are the one projection not rebuilt here: publishing a mosaic
is a two-phase filesystem+DB protocol (see forestgate/mosaic.py), so replay
cannot reproduce its crash/recovery history.  Mosaic requests are replayed
as evidence bytes only, and the live mosaic chain (current pointer ->
publish record -> build -> manifest -> artifact -> source tile bytes) is
verified by hash instead.
"""
import hashlib,tempfile
from contextlib import closing
from pathlib import Path
from .store import connect,report
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .hotspot import import_tile
from . import mosaic

def mosaic_check(db_path):
    with closing(connect(db_path)) as db:
        builds=db.execute('SELECT COUNT(*) AS c FROM mosaic_build').fetchone()['c']
        meta=db.execute("SELECT value FROM mosaic_meta WHERE key='out_dir'").fetchone()
    if not builds:return {'status':'absent'}
    if not meta:return {'status':'tampered','problems':['out_dir_unknown']}
    return mosaic.verify(db_path,meta['value'])

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
            {'station':import_station,'station_revoke':revoke_station,'risk':import_risk,'sample':import_sample,'shift':import_shift,'patrol':import_patrol,'access':import_access,'recheck':recheck_access,'field_challenge':import_field_challenge,'tile':import_tile,'mosaic':mosaic.register_request}[item['kind']](rebuilt,file)
        replay=report(rebuilt)
    fields=('blocks','stations','risk','samples','conflicts','shifts','fires','access','rechecks','challenges','challenge_states','tiles')
    result={'status':'matched' if all(original[k]==replay[k] for k in fields) else 'divergent',
            'source_count':len(sources),'differences':[k for k in fields if original[k]!=replay[k]]}
    result['mosaic']=mosaic_check(db_path)
    if result['mosaic']['status'] not in ('absent','ok'):
        result['status']='divergent';result['differences'].append('mosaic')
    return result
