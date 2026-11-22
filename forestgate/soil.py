"""A provisional capacitance-only nursery moisture reading."""
from contextlib import closing
from .store import connect, source


def import_plot(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'nursery_plot')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if doc['area_m2'] <= 0:
            raise ValueError('invalid_area')
        db.execute('INSERT INTO nursery_plot VALUES(?,?,?,?)',
                   (doc['plot_id'], doc['block_id'], doc['area_m2'], doc['id']))
        return {'plot_id': doc['plot_id']}


def import_reading(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'soil_reading')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        plot = db.execute('SELECT * FROM nursery_plot WHERE id=?', (doc['plot_id'],)).fetchone()
        if plot is None or not 0 <= doc['capacitance'] <= 100:
            raise ValueError('invalid_reading')
        db.execute('INSERT INTO soil_reading VALUES(?,?,?,?)',
                   (doc['id'], doc['plot_id'], doc['at'], doc['capacitance']))
        from .irrigation import advise
        return advise(db, doc['plot_id'])
