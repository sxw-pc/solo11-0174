"""Capture independent receiver pulses and expose strike evidence.

Two ingest paths share this module:

* legacy burst pulses (``burst_id`` only): kept for the fixed baseline where the
  third pulse of a burst writes the earliest receiver's coordinates into
  ``lightning_locate`` with method ``first_arrival``. That value is only a coarse
  provisional hint; it is never a confirmed strike point.
* waveform pulses (``boot_id`` + ``waveform`` fragment): pulses are grouped into
  strike events by waveform similarity and the physical maximum propagation time
  between receivers. Page arrival order and claimed burst ids are deliberately
  ignored because pulse ids may be mismatched between receivers and pages may
  arrive late.

Each receiver's clock offset is a per-boot-session shared parameter registered
through ``lightning_clock`` sources; recalibration appends a new version and may
only narrow the uncertainty, never silently rewrite the old evidence.
"""
import json
import math
from contextlib import closing
from .store import connect, log, source

WAVEFORM_MIN_SIMILARITY = 0.95
GROUP_PHYSICAL_SLACK_US = 0.5
CLOCK_SOURCES = ('gnss', 'free', 'gnss_recovered')


def import_site(db_path, file):
    """Register the versioned coordinate grid, time unit and propagation speed."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lightning_site')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        speed = doc.get('speed_km_per_us')
        if not doc.get('coord_version') or doc.get('time_unit') != 'us' \
                or not isinstance(speed, (int, float)) or speed <= 0:
            raise ValueError('site_contract_invalid')
        if db.execute('SELECT 1 FROM lightning_site').fetchone():
            raise ValueError('site_already_registered')
        db.execute('INSERT INTO lightning_site VALUES(?,?,?,?,?)',
                   (doc['id'], doc['coord_version'], doc['time_unit'], speed, doc['id']))
        log(db, 'lightning_site', doc['id'], 'registered', doc['coord_version'], doc['id'])
        return {'site_id': doc['id'], 'coord_version': doc['coord_version']}


def import_receiver(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lightning_receiver')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['receiver_id']:
            raise ValueError('receiver_id_required')
        site = db.execute('SELECT * FROM lightning_site').fetchone()
        if site and doc.get('coord_version') and doc['coord_version'] != site['coord_version']:
            raise ValueError('receiver_coord_version_mismatch')
        db.execute('INSERT INTO lightning_receiver VALUES(?,?,?,?,?)',
                   (doc['receiver_id'], doc['x_km'], doc['y_km'], doc['clock_offset_us'], doc['id']))
        return {'receiver_id': doc['receiver_id']}


def import_clock(db_path, file):
    """Register clock-sync evidence for one receiver boot session.

    The offset is shared by every pulse of the same boot; a later source only
    adds a narrower version (e.g. a recovered GNSS log), the old version stays.
    """
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lightning_clock')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not db.execute('SELECT 1 FROM lightning_receiver WHERE id=?', (doc.get('receiver_id'),)).fetchone():
            raise ValueError('unknown_receiver')
        unc = doc.get('uncertainty_us')
        if doc.get('clock_source') not in CLOCK_SOURCES or not doc.get('boot_id') \
                or not isinstance(unc, (int, float)) or unc < 0 or not doc.get('boot_started_at'):
            raise ValueError('clock_contract_invalid')
        prior = db.execute('SELECT * FROM lightning_clock WHERE receiver_id=? AND boot_id=? ORDER BY version DESC',
                           (doc['receiver_id'], doc['boot_id'])).fetchall()
        if prior and unc > prior[0]['uncertainty_us'] + 1e-9:
            raise ValueError('clock_uncertainty_widening')
        version = (prior[0]['version'] + 1) if prior else 1
        db.execute('INSERT INTO lightning_clock VALUES(?,?,?,?,?,?,?,?,?)',
                   (doc['id'], doc['receiver_id'], doc['boot_id'], doc['clock_source'], doc['offset_us'],
                    unc, doc['boot_started_at'], doc.get('evidence', ''), version))
        log(db, 'lightning_clock', f"{doc['receiver_id']}/{doc['boot_id']}", f'v{version}', doc['clock_source'], doc['id'])
        return {'receiver_id': doc['receiver_id'], 'boot_id': doc['boot_id'], 'version': version}


def import_compartment(db_path, file):
    """Register a forest compartment polygon (in the site's coordinate version)."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lightning_compartment')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        poly = doc.get('polygon')
        ok = isinstance(poly, list) and len(poly) >= 3 and all(
            isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p) for p in poly)
        if not doc.get('compartment_id') or not doc.get('block_id') or not ok:
            raise ValueError('compartment_contract_invalid')
        db.execute('INSERT INTO lightning_compartment VALUES(?,?,?,?)',
                   (doc['compartment_id'], doc['block_id'], json.dumps(poly), doc['id']))
        return {'compartment_id': doc['compartment_id']}


def import_pulse(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lightning_pulse')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        receiver = db.execute('SELECT * FROM lightning_receiver WHERE id=?',
                              (doc['receiver_id'],)).fetchone()
        if receiver is None or doc['arrival_us'] < 0 or doc['amplitude'] <= 0:
            raise ValueError('invalid_pulse')
        if doc.get('boot_id') is not None or doc.get('waveform') is not None:
            return _import_waveform_pulse(db, doc)
        db.execute('INSERT INTO lightning_pulse(source_id,burst_id,receiver_id,arrival_us,amplitude) VALUES(?,?,?,?,?)',
                   (doc['id'], doc['burst_id'], doc['receiver_id'], doc['arrival_us'], doc['amplitude']))
        pulses = db.execute('''SELECT p.*, r.x_km, r.y_km FROM lightning_pulse p
            JOIN lightning_receiver r ON r.id=p.receiver_id WHERE p.burst_id=?''',
                            (doc['burst_id'],)).fetchall()
        if len({p['receiver_id'] for p in pulses}) >= 3:
            first = min(pulses, key=lambda p: p['arrival_us'])
            db.execute('INSERT INTO lightning_locate VALUES(?,?,?,?) ON CONFLICT(burst_id) DO UPDATE SET x_km=excluded.x_km,y_km=excluded.y_km',
                       (doc['burst_id'], first['x_km'], first['y_km'], 'first_arrival'))
        return {'burst_id': doc['burst_id'], 'pulse_count': len(pulses)}


def _import_waveform_pulse(db, doc):
    site = db.execute('SELECT * FROM lightning_site').fetchone()
    if site is None:
        raise ValueError('site_required')
    clock = _current_clock(db, doc['receiver_id'], doc.get('boot_id'))
    waveform = doc.get('waveform')
    if clock is None or not isinstance(waveform, list) or len(waveform) < 4 \
            or not all(isinstance(v, (int, float)) for v in waveform):
        raise ValueError('invalid_waveform_pulse')
    db.execute('INSERT INTO lightning_pulse(source_id,burst_id,receiver_id,arrival_us,amplitude,boot_id,page_seq,waveform) VALUES(?,?,?,?,?,?,?,?)',
               (doc['id'], doc.get('burst_id'), doc['receiver_id'], doc['arrival_us'], doc['amplitude'],
                doc['boot_id'], doc.get('page_seq'), json.dumps(waveform)))
    event_id = _assign_event(db, doc, site['speed_km_per_us'])
    count = db.execute('SELECT COUNT(*) FROM lightning_event_pulse WHERE event_id=?', (event_id,)).fetchone()[0]
    return {'event_id': event_id, 'event_pulses': count}


def _current_clock(db, receiver_id, boot_id):
    return db.execute('''SELECT * FROM lightning_clock WHERE receiver_id=? AND boot_id=?
        ORDER BY version DESC LIMIT 1''', (receiver_id, boot_id)).fetchone()


def _similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _physically_compatible(db, doc, member, speed):
    rx_new = db.execute('SELECT * FROM lightning_receiver WHERE id=?', (doc['receiver_id'],)).fetchone()
    rx_old = db.execute('SELECT * FROM lightning_receiver WHERE id=?', (member['receiver_id'],)).fetchone()
    clock_new = _current_clock(db, doc['receiver_id'], doc['boot_id'])
    clock_old = _current_clock(db, member['receiver_id'], member['boot_id'])
    gap_us = math.hypot(rx_new['x_km'] - rx_old['x_km'], rx_new['y_km'] - rx_old['y_km']) / speed
    slack = clock_new['uncertainty_us'] + clock_old['uncertainty_us'] + GROUP_PHYSICAL_SLACK_US
    return abs(doc['arrival_us'] - member['arrival_us']) <= gap_us + slack


def _assign_event(db, doc, speed):
    """Attach a pulse to the best evidence-compatible event, else open a new one.

    Compatibility requires a similar waveform and a physically possible arrival
    gap against every member; page order and claimed burst ids play no role.
    """
    best_id, best_score = None, -1.0
    for event in db.execute('SELECT * FROM lightning_event ORDER BY seq').fetchall():
        members = db.execute('''SELECT ep.*, p.waveform FROM lightning_event_pulse ep
            JOIN lightning_pulse p ON p.source_id=ep.pulse_source_id WHERE ep.event_id=?''',
                             (event['id'],)).fetchall()
        if any(m['receiver_id'] == doc['receiver_id'] for m in members):
            continue
        sims = []
        for m in members:
            sim = _similarity(json.loads(m['waveform']), doc['waveform'])
            if sim < WAVEFORM_MIN_SIMILARITY or not _physically_compatible(db, doc, m, speed):
                sims = []
                break
            sims.append(sim)
        if sims and min(sims) > best_score:
            best_id, best_score = event['id'], min(sims)
    if best_id is None:
        seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM lightning_event').fetchone()[0]
        best_id = f'evt-{seq}'
        db.execute('INSERT INTO lightning_event VALUES(?,?,?,?)', (best_id, seq, 'partial', 0))
    db.execute('INSERT INTO lightning_event_pulse VALUES(?,?,?,?,?)',
               (best_id, doc['id'], doc['receiver_id'], doc['boot_id'], doc['arrival_us']))
    count = db.execute('SELECT COUNT(*) FROM lightning_event_pulse WHERE event_id=?', (best_id,)).fetchone()[0]
    db.execute('UPDATE lightning_event SET receiver_count=?, status=? WHERE id=?',
               (count, 'grouped' if count >= 3 else 'partial', best_id))
    return best_id
