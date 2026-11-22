"""Capture camera traps and tally provisional local track hints."""
from contextlib import closing
from .store import connect, source


def import_camera(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'wildlife_camera')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['block_id'] or not doc['camera_id']:
            raise ValueError('invalid_camera')
        db.execute('INSERT INTO wildlife_camera VALUES(?,?,?,?,?)',
                   (doc['camera_id'], doc['block_id'], doc['x_km'], doc['y_km'], doc['id']))
        return {'camera_id': doc['camera_id']}


def import_frame(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'wildlife_frame')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        camera = db.execute('SELECT * FROM wildlife_camera WHERE id=?',
                            (doc['camera_id'],)).fetchone()
        if camera is None or not doc['track_hint'] or not 0 <= doc['quality'] <= 1:
            raise ValueError('invalid_frame')
        db.execute('INSERT INTO wildlife_frame VALUES(?,?,?,?,?,?)',
                   (doc['id'], doc['camera_id'], doc['species'], doc['track_hint'],
                    doc['at'], doc['quality']))
        count = db.execute('''SELECT COUNT(DISTINCT f.camera_id||':'||f.track_hint)
            FROM wildlife_frame f JOIN wildlife_camera c ON c.id=f.camera_id
            WHERE c.block_id=? AND f.species=?''', (camera['block_id'], doc['species'])).fetchone()[0]
        db.execute('INSERT INTO wildlife_count VALUES(?,?,?) ON CONFLICT(block_id,species) DO UPDATE SET count=excluded.count',
                   (camera['block_id'], doc['species'], count))
        return {'block_id': camera['block_id'], 'species': doc['species'], 'provisional_count': count}
