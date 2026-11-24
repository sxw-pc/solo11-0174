"""Capture plot and laser returns; heights remain relative to unknown ground.

Returns are stored as raw surface observations only.  Beam and return order
carry no ground/canopy classification: on a slope or above low shrubs the
last return of a beam may still be vegetation, never proven ground.
"""
from contextlib import closing
from .store import connect, source


def _import_plot_doc(db, doc, source_id):
    if doc['area_m2'] <= 0:
        raise ValueError('invalid_area')
    db.execute('INSERT INTO lidar_plot VALUES(?,?,?,?)',
               (doc['plot_id'], doc['block_id'], doc['area_m2'], source_id))
    from .stand_volume import reassess_for_block
    reassess_for_block(db, doc['block_id'], source_id)
    return {'plot_id': doc['plot_id']}


def import_plot(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lidar_plot')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _import_plot_doc(db, doc, doc['id'])


def _import_return_doc(db, doc, source_id):
    plot = db.execute('SELECT * FROM lidar_plot WHERE id=?', (doc['plot_id'],)).fetchone()
    if plot is None or doc['return_no'] < 1:
        raise ValueError('invalid_return')
    db.execute('INSERT INTO lidar_return VALUES(?,?,?,?,?)',
               (source_id, doc['plot_id'], doc['beam_id'], doc['z_m'], doc['return_no']))
    from .timber import estimate
    value = estimate(db, doc['plot_id'])
    from .stand_volume import reassess_for_block
    reassess_for_block(db, plot['block_id'], source_id)
    return value


def import_return(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lidar_return')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _import_return_doc(db, doc, doc['id'])
