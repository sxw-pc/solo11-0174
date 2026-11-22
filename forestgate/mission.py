"""Reserve a mission against one depot's currently declared stock."""
from contextlib import closing
from .store import connect, source

def reserve_mission(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'fuel_mission')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        depot = db.execute('SELECT * FROM fuel_depot WHERE id=?', (doc['depot_id'],)).fetchone()
        if not depot or depot['block_id'] != doc['block_id']:
            raise ValueError('fuel_depot_unknown')
        need = doc['need_l']
        if not isinstance(need, int) or need <= 0:
            raise ValueError('mission_need_invalid')
        status = 'reserved' if depot['stock_l'] >= need else 'short'
        if status == 'reserved':
            db.execute('UPDATE fuel_depot SET stock_l=stock_l-? WHERE id=?', (need, depot['id']))
        db.execute('INSERT INTO fuel_mission VALUES(?,?,?,?,?,?)',
                   (doc['mission_id'], doc['block_id'], doc['depot_id'], need, status, doc['id']))
        return {'mission_id': doc['mission_id'], 'status': status}
