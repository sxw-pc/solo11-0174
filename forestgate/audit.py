"""Rebuild current forest access decisions from immutable input bytes."""
import hashlib,tempfile
from contextlib import closing
from pathlib import Path
from .store import connect,report
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .soil import (import_plot,import_reading,import_calibration,import_probe,
                   import_tank,import_dual_frequency_scan,import_soil_sample)

IMPORTERS={'station':import_station,'station_revoke':revoke_station,'risk':import_risk,
           'sample':import_sample,'shift':import_shift,'patrol':import_patrol,
           'access':import_access,'recheck':recheck_access,'field_challenge':import_field_challenge,
           'nursery_plot':import_plot,'soil_reading':import_reading,
           'calibration_batch':import_calibration,'probe':import_probe,'water_tank':import_tank,
           'dual_frequency_scan':import_dual_frequency_scan,'soil_sample':import_soil_sample}

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
            IMPORTERS[item['kind']](rebuilt,file)
        replay=report(rebuilt)
    # moisture_plans and plan_* projections embed an import timestamp and are
    # rebuilt explicitly via moisture_bounds.build_plan; the evidence-fed base
    # tables below must replay byte-identically from the original JSON bytes.
    fields=('blocks','stations','risk','samples','conflicts','shifts','fires','access','rechecks',
            'challenges','challenge_states','nursery_plots','soil_readings','irrigation_advice',
            'calibration_batches','probes','water_tanks','dual_frequency_scans',
            'dual_frequency_cells','soil_samples')
    return {'status':'matched' if all(original[k]==replay[k] for k in fields) else 'divergent',
            'source_count':len(sources),'differences':[k for k in fields if original[k]!=replay[k]]}
