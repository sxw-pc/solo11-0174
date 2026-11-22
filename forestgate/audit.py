"""Rebuild current forest access decisions from immutable input bytes."""
import hashlib,tempfile
from contextlib import closing
from pathlib import Path
from .store import connect,report
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .lightning import import_site,import_receiver,import_clock,import_compartment,import_pulse
from .strike_area import publish_release

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
            {'station':import_station,'station_revoke':revoke_station,'risk':import_risk,'sample':import_sample,'shift':import_shift,'patrol':import_patrol,'access':import_access,'recheck':recheck_access,'field_challenge':import_field_challenge,'lightning_site':import_site,'lightning_receiver':import_receiver,'lightning_clock':import_clock,'lightning_compartment':import_compartment,'lightning_pulse':import_pulse,'lightning_release':publish_release}[item['kind']](rebuilt,file)
        replay=report(rebuilt)
    fields=('blocks','stations','risk','samples','conflicts','shifts','fires','access','rechecks','challenges','challenge_states',
            'lightning_receivers','lightning_pulses','lightning_locations','lightning_sites','lightning_clocks','lightning_compartments',
            'lightning_events','lightning_event_pulses','lightning_releases','lightning_release_events','lightning_release_compartments','lightning_release_todos')
    return {'status':'matched' if all(original[k]==replay[k] for k in fields) else 'divergent',
            'source_count':len(sources),'differences':[k for k in fields if original[k]!=replay[k]]}
