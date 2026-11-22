"""Record a local emergency fuel depot's declared opening stock."""
from contextlib import closing
from .store import connect, source

def register_depot(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'fuel_depot')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        capacity, stock = doc['capacity_l'], doc['stock_l']
        if not isinstance(capacity, int) or not isinstance(stock, int) or not 0 <= stock <= capacity:
            raise ValueError('fuel_capacity_invalid')
        db.execute('INSERT INTO fuel_depot VALUES(?,?,?,?,?)',
                   (doc['depot_id'], doc['block_id'], capacity, stock, doc['id']))
        return {'depot_id': doc['depot_id'], 'stock_l': stock}
