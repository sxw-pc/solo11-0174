"""Classify a single-band-triplet field scan against registered references."""
import json
from contextlib import closing
from .store import connect, source

def import_scan(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'fuel_scan')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        bands = doc.get('bands')
        if not isinstance(bands, list) or len(bands) != 3 or any(not isinstance(x, int) or x < 0 for x in bands):
            raise ValueError('scan_bands_invalid')
        rows = db.execute('SELECT * FROM fuel_signature ORDER BY id').fetchall()
        if not rows:
            raise ValueError('no_signatures')
        choices = [(sum(abs(a-b) for a, b in zip(bands, json.loads(row['bands']))), row['material']) for row in rows]
        distance, material = min(choices)
        db.execute('INSERT INTO fuel_scan VALUES(?,?,?,?,?)',
                   (doc['id'], doc['block_id'], json.dumps(bands), material, distance))
        return {'id': doc['id'], 'material': material, 'distance': distance}
