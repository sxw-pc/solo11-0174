"""Capture independent receiver pulses and expose a provisional strike location."""
from contextlib import closing
from .store import connect, source


def import_receiver(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lightning_receiver')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['receiver_id']:
            raise ValueError('receiver_id_required')
        db.execute('INSERT INTO lightning_receiver VALUES(?,?,?,?,?)',
                   (doc['receiver_id'], doc['x_km'], doc['y_km'], doc['clock_offset_us'], doc['id']))
        return {'receiver_id': doc['receiver_id']}


def import_pulse(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lightning_pulse')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        receiver = db.execute('SELECT * FROM lightning_receiver WHERE id=?',
                              (doc['receiver_id'],)).fetchone()
        if receiver is None or doc['arrival_us'] < 0 or doc['amplitude'] <= 0:
            raise ValueError('invalid_pulse')
        db.execute('INSERT INTO lightning_pulse VALUES(?,?,?,?,?)',
                   (doc['id'], doc['burst_id'], doc['receiver_id'], doc['arrival_us'], doc['amplitude']))
        pulses = db.execute('''SELECT p.*, r.x_km, r.y_km FROM lightning_pulse p
            JOIN lightning_receiver r ON r.id=p.receiver_id WHERE p.burst_id=?''',
                            (doc['burst_id'],)).fetchall()
        if len({p['receiver_id'] for p in pulses}) >= 3:
            first = min(pulses, key=lambda p: p['arrival_us'])
            db.execute('INSERT INTO lightning_locate VALUES(?,?,?,?) ON CONFLICT(burst_id) DO UPDATE SET x_km=excluded.x_km,y_km=excluded.y_km',
                       (doc['burst_id'], first['x_km'], first['y_km'], 'first_arrival'))
        return {'burst_id': doc['burst_id'], 'pulse_count': len(pulses)}
