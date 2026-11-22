"""Record a reference spectrum for a known forest-floor material."""
import json
from contextlib import closing
from .store import connect, source

def import_signature(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'signature')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        bands = doc.get('bands')
        if not isinstance(bands, list) or len(bands) != 3 or any(not isinstance(x, int) or x < 0 for x in bands):
            raise ValueError('signature_bands_invalid')
        db.execute('INSERT INTO fuel_signature VALUES(?,?,?,?)',
                   (doc['signature_id'], doc['material'], json.dumps(bands), doc['id']))
        return {'signature_id': doc['signature_id'], 'status': 'registered'}
