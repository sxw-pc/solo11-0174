"""Capture offline hotspot tile summaries before spatial review."""
from contextlib import closing
from .store import connect, source

def import_tile(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'tile')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc.get('track_id') or not doc.get('block_hint') or not doc.get('captured_at'):
            raise ValueError('tile_identity_missing')
        hot, cloud = doc.get('hot_pixels'), doc.get('cloud_pixels')
        if not isinstance(hot, int) or not isinstance(cloud, int) or hot < 0 or cloud < 0:
            raise ValueError('tile_counts_invalid')
        db.execute('INSERT INTO tile_capture VALUES(?,?,?,?,?,?)',
                   (doc['id'], doc['track_id'], doc['block_hint'], doc['captured_at'], hot, cloud))
        return {'id': doc['id'], 'status': 'captured', 'hot_pixels': hot}
