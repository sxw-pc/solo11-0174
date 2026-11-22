"""Declared opening stock plus tank geometry, dipstick and attitude evidence.

Two ledgers live side by side and never overwrite each other:

* the *declaration ledger* (``fuel_depot`` / ``fuel_mission``) keeps the
  opening ``stock_l`` claim and the original reservation, exactly as the
  baseline replay requires;
* the *physical evidence ledger* (geometry, dips, tilts, level transfers and
  versioned corroborations) integrates tank-body coordinates and records what
  is actually guaranteed. A single dipstick cannot collapse the tilted-tank
  range to one number, and late attitude evidence only adds a corroboration
  version for the matching reading; it never rewrites history.
"""
from contextlib import closing

from .store import connect, source, log
from .dip_volume import (
    TankGeometry,
    gross_range_l,
    extractable_range_l,
    dip_only_ranges_l,
    whole_litre_bounds,
)


# ---------------------------------------------------------------------------
# Shared lookups
# ---------------------------------------------------------------------------

def _require_depot(db, depot_id, block_id=None):
    depot = db.execute('SELECT * FROM fuel_depot WHERE id=?', (depot_id,)).fetchone()
    if not depot or (block_id is not None and depot['block_id'] != block_id):
        raise ValueError('fuel_depot_unknown')
    return depot


def _geometry(db, depot_id):
    row = db.execute('SELECT * FROM tank_geometry WHERE depot_id=?', (depot_id,)).fetchone()
    if not row:
        raise ValueError('tank_geometry_unknown')
    return row, TankGeometry(
        row['length_m'], row['width_m'], row['height_m'], row['dead_m'],
        row['slope_min'], row['slope_max'])


def _next_corroboration_version(db, dip_id):
    row = db.execute('SELECT COALESCE(MAX(version),0)+1 AS v FROM dip_corroboration WHERE dip_id=?',
                     (dip_id,)).fetchone()
    return row['v']


def _store_corroboration(db, dip, kind, source_id, slope=None, attitude_source_id=None):
    _, geometry = _geometry(db, dip['depot_id'])
    h, h_err = dip['head_depth_m'], dip['depth_error_m']
    if kind == 'tilt':
        tilt = db.execute('SELECT * FROM tank_tilt WHERE source_id=?', (attitude_source_id,)).fetchone()
        gross_low, gross_high = gross_range_l(geometry, h, h_err, tilt['slope'], tilt['slope_error_m'])
        ext_low, ext_high = extractable_range_l(geometry, h, h_err, tilt['slope'], tilt['slope_error_m'])
        slope = tilt['slope']
    else:
        ranges = dip_only_ranges_l(geometry, h, h_err)
        gross_low, gross_high = ranges['gross_low_l'], ranges['gross_high_l']
        ext_low, ext_high = ranges['extractable_low_l'], ranges['extractable_high_l']
    version = _next_corroboration_version(db, dip['id'])
    db.execute(
        'INSERT INTO dip_corroboration(dip_id,version,kind,slope,attitude_source_id,'
        'gross_low_l,gross_high_l,extractable_low_l,extractable_high_l,source_id) '
        'VALUES(?,?,?,?,?,?,?,?,?,?)',
        (dip['id'], version, kind, slope, attitude_source_id,
         gross_low, gross_high, ext_low, ext_high, source_id))
    return version, {
        'dip_id': dip['id'], 'version': version, 'kind': kind, 'slope': slope,
        'gross_l': whole_litre_bounds(gross_low, gross_high),
        'extractable_l': whole_litre_bounds(ext_low, ext_high),
    }


def _matching_tilt(db, dip):
    """Same-instant attitude record; the leg epoch must match the dip's."""
    return db.execute(
        'SELECT * FROM tank_tilt WHERE depot_id=? AND epoch=? AND observed_at=? '
        'ORDER BY source_id',
        (dip['depot_id'], dip['epoch'], dip['observed_at'])).fetchall()


def _corroborate_dip(db, dip, source_id):
    """Attach every applicable attitude version, else an un-attested range."""
    tilts = _matching_tilt(db, dip)
    results = []
    if tilts:
        for tilt in tilts:
            _, summary = _store_corroboration(
                db, dip, 'tilt', tilt['source_id'],
                attitude_source_id=tilt['source_id'])
            results.append(summary)
    else:
        _, summary = _store_corroboration(db, dip, 'dip_only', source_id)
        results.append(summary)
    # Imported lazily to keep the mission <-> fuel dependency one-directional.
    from .mission import _revise_missions_for_dip
    for tilt in tilts:
        _revise_missions_for_dip(db, dip, tilt['source_id'])
    return results


# ---------------------------------------------------------------------------
# Declaration ledger (baseline behaviour preserved)
# ---------------------------------------------------------------------------

def _apply_depot(db, doc, source_id):
    capacity, stock = doc['capacity_l'], doc['stock_l']
    if not isinstance(capacity, int) or not isinstance(stock, int) or not 0 <= stock <= capacity:
        raise ValueError('fuel_capacity_invalid')
    db.execute('INSERT INTO fuel_depot VALUES(?,?,?,?,?)',
               (doc['depot_id'], doc['block_id'], capacity, stock, source_id))
    log(db, 'fuel', doc['depot_id'], 'declared', 'opening_stock_declared', source_id)
    return {'depot_id': doc['depot_id'], 'stock_l': stock}


def register_depot(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'fuel_depot')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        result = _apply_depot(db, doc, doc['id'])
        return result


# ---------------------------------------------------------------------------
# Physical evidence: geometry, dipsticks, attitude, levelled transfers
# ---------------------------------------------------------------------------

def _apply_geometry(db, doc, source_id):
    depot = _require_depot(db, doc['depot_id'], doc.get('block_id'))
    fields = ('length_m', 'width_m', 'height_m')
    if any(not isinstance(doc[f], (int, float)) or doc[f] <= 0 for f in fields):
        raise ValueError('geometry_dimensions_invalid')
    dead = doc.get('dead_m', 0.0)
    band = doc.get('slope_band', [-0.10, 0.10])
    if not isinstance(dead, (int, float)) or not 0 <= dead <= doc['height_m']:
        raise ValueError('dead_level_invalid')
    if (not isinstance(band, list) or len(band) != 2 or band[0] > band[1]):
        raise ValueError('slope_band_invalid')
    # Validate eagerly via the value object.
    TankGeometry(doc['length_m'], doc['width_m'], doc['height_m'], dead, band[0], band[1])
    db.execute(
        'INSERT INTO tank_geometry(id,depot_id,length_m,width_m,height_m,'
        'dead_m,slope_min,slope_max,source_id) VALUES(?,?,?,?,?,?,?,?,?)',
        (doc['geometry_id'], depot['id'], doc['length_m'], doc['width_m'],
         doc['height_m'], dead, band[0], band[1], source_id))
    return {'geometry_id': doc['geometry_id'], 'depot_id': depot['id']}


def import_geometry(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'tank_geometry')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _apply_geometry(db, doc, doc['id'])


def _apply_dip(db, doc, source_id):
    depot = _require_depot(db, doc['depot_id'], doc.get('block_id'))
    row, geometry = _geometry(db, depot['id'])
    h, h_err = doc['head_depth_m'], doc.get('depth_error_m', 0.0)
    if not isinstance(h, (int, float)) or not 0 <= h <= geometry.height_m:
        raise ValueError('dip_depth_invalid')
    if not isinstance(h_err, (int, float)) or h_err < 0:
        raise ValueError('dip_error_invalid')
    if not doc.get('epoch') or not doc.get('observed_at'):
        raise ValueError('dip_epoch_or_time_missing')
    db.execute(
        'INSERT INTO tank_dip(id,depot_id,epoch,observed_at,head_depth_m,'
        'depth_error_m,source_id) VALUES(?,?,?,?,?,?,?)',
        (doc['dip_id'], depot['id'], doc['epoch'], doc['observed_at'], h, h_err, source_id))
    dip = db.execute('SELECT * FROM tank_dip WHERE id=?', (doc['dip_id'],)).fetchone()
    summaries = _corroborate_dip(db, dip, source_id)
    return {'dip_id': doc['dip_id'], 'corroborations': summaries,
            'status': summaries[-1]['kind']}


def import_dip(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'tank_dip')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _apply_dip(db, doc, doc['id'])


def _apply_tilt(db, doc, source_id):
    depot = _require_depot(db, doc['depot_id'], doc.get('block_id'))
    _, geometry = _geometry(db, depot['id'])
    slope, s_err = doc['slope'], doc.get('slope_error_m', 0.0)
    if not isinstance(slope, (int, float)) or not geometry.slope_min <= slope <= geometry.slope_max:
        raise ValueError('tilt_outside_approved_band')
    if not isinstance(s_err, (int, float)) or s_err < 0:
        raise ValueError('tilt_error_invalid')
    if not doc.get('epoch') or not doc.get('observed_at'):
        raise ValueError('tilt_epoch_or_time_missing')
    conflict = db.execute(
        'SELECT slope FROM tank_tilt WHERE depot_id=? AND epoch=? AND observed_at=?',
        (depot['id'], doc['epoch'], doc['observed_at'])).fetchall()
    if any(abs(row['slope'] - slope) > 1e-12 for row in conflict):
        raise ValueError('tilt_conflict')
    db.execute(
        'INSERT INTO tank_tilt(source_id,depot_id,epoch,observed_at,slope,'
        'slope_error_m) VALUES(?,?,?,?,?,?)',
        (source_id, depot['id'], doc['epoch'], doc['observed_at'], slope, s_err))
    # Late attitude evidence only opens a new version for the readings taken
    # at that same instant, inside the same leg epoch.
    revised = []
    dips = db.execute(
        'SELECT * FROM tank_dip WHERE depot_id=? AND epoch=? AND observed_at=? ORDER BY id',
        (depot['id'], doc['epoch'], doc['observed_at'])).fetchall()
    for dip in dips:
        _, summary = _store_corroboration(
            db, dip, 'tilt', source_id, attitude_source_id=source_id)
        from .mission import _revise_missions_for_dip
        _revise_missions_for_dip(db, dip, source_id)
        revised.append(summary)
    return {'tilt_source_id': source_id, 'revised_dips': revised}


def import_tilt(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'tank_tilt')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _apply_tilt(db, doc, doc['id'])


def _apply_transfer(db, doc, source_id):
    depot = _require_depot(db, doc['depot_id'], doc.get('block_id'))
    amount, error = doc['extractable_l'], doc.get('error_l', 0)
    if not isinstance(amount, int) or amount < 0 or not isinstance(error, int) or error < 0:
        raise ValueError('transfer_measure_invalid')
    if amount > depot['capacity_l']:
        raise ValueError('transfer_exceeds_capacity')
    if not doc.get('epoch') or not doc.get('observed_at'):
        raise ValueError('transfer_epoch_or_time_missing')
    db.execute(
        'INSERT INTO level_transfer(source_id,depot_id,epoch,observed_at,'
        'extractable_l,metre_error_m) VALUES(?,?,?,?,?,?)',
        (source_id, depot['id'], doc['epoch'], doc['observed_at'], amount, error))
    log(db, 'fuel', depot['id'], 'transfer_measured',
        f'levelled_extractable_{amount}l', source_id)
    return {'transfer_source_id': source_id, 'extractable_l': amount,
            'extractable_high_l': amount + error}


def import_level_transfer(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'level_transfer')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _apply_transfer(db, doc, doc['id'])


# ---------------------------------------------------------------------------
# Batched evidence fixture (one immutable source carrying several records)
# ---------------------------------------------------------------------------

_APPLIERS = {
    'fuel_depot': _apply_depot,
    'tank_geometry': _apply_geometry,
    'tank_dip': _apply_dip,
    'tank_tilt': _apply_tilt,
    'level_transfer': _apply_transfer,
}


def import_evidence_batch(db_path, file):
    # Lazy import: mission depends on fuel, never the other way around at
    # module load time.
    from .mission import _apply_mission

    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'evidence_batch')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not isinstance(doc.get('items'), list) or not doc['items']:
            raise ValueError('evidence_batch_empty')
        item_ids = [item.get('id') for item in doc['items']]
        if any(not isinstance(item_id, str) or not item_id for item_id in item_ids):
            raise ValueError('evidence_batch_item_id_missing')
        if len(set(item_ids)) != len(item_ids):
            raise ValueError('evidence_batch_duplicate_item_id')
        appliers = dict(_APPLIERS)
        appliers['fuel_mission'] = _apply_mission
        applied = []
        for item in doc['items']:
            kind = item.get('kind')
            if kind not in appliers:
                raise ValueError('evidence_batch_unsupported_kind')
            applied.append({'kind': kind, 'result': appliers[kind](db, item, doc['id'])})
        return {'batch_id': doc['id'], 'applied': applied}
