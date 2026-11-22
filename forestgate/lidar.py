"""Capture plot and laser returns; heights remain relative to unknown ground."""
from contextlib import closing
from .store import connect, source


def import_plot(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lidar_plot')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if doc['area_m2'] <= 0:
            raise ValueError('invalid_area')
        db.execute('INSERT INTO lidar_plot VALUES(?,?,?,?)',
                   (doc['plot_id'], doc['block_id'], doc['area_m2'], doc['id']))
        return {'plot_id': doc['plot_id']}


def import_return(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lidar_return')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        plot = db.execute('SELECT * FROM lidar_plot WHERE id=?', (doc['plot_id'],)).fetchone()
        if plot is None or doc['return_no'] < 1:
            raise ValueError('invalid_return')
        db.execute('INSERT INTO lidar_return VALUES(?,?,?,?,?)',
                   (doc['id'], doc['plot_id'], doc['beam_id'], doc['z_m'], doc['return_no']))
        from .timber import estimate
        return estimate(db, doc['plot_id'])
