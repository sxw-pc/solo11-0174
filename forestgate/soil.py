"""Nursery evidence intake.

The original capacitance intake (``import_plot`` / ``import_reading``) is kept
byte-for-byte as the single-frequency original: a reading still projects the
provisional capacitance-only advice through ``irrigation.advise``.  The saline
season adds five further, independently hashed evidence kinds:

* calibration_batch - a batch-wide baseline salt offset shared by every probe
  in the batch (the offset cannot be picked per plot);
* probe - frequency, temperature, batch membership and salt-sensitivity curve;
* water_tank - remaining capacity and the adjacent plots it serves;
* dual_frequency_scan - a second-frequency D=W-S sweep over bounded cells,
  with shaded ridges explicitly recorded as not covered;
* soil_sample - an independent oven-dry sample on a fixed bounded grid cell.
"""
import json
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


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _polygon_area(bounds_m2):
    """Shoelace area for a simple polygon given as [[x_m, y_m], ...]."""
    pts = bounds_m2
    if not isinstance(pts, list) or len(pts) < 3:
        raise ValueError('invalid_bounds_polygon')
    total = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        total += x1 * y2 - y1 * x2
    return abs(total) / 2.0


def _bounds_area(doc, area_key, bounds_key):
    declared = doc[area_key]
    _require(isinstance(declared, (int, float)) and declared > 0, 'invalid_area')
    if bounds_key in doc:
        measured = _polygon_area(doc[bounds_key])
        _require(abs(measured - float(declared)) <= 1e-6, 'area_bounds_mismatch')
    return float(declared)


def import_calibration(db_path, file):
    """Register a calibration batch whose salt offset is shared batch-wide."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'calibration_batch')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        lo, hi = doc['offset_min'], doc['offset_max']
        _require(isinstance(lo, (int, float)) and isinstance(hi, (int, float))
                 and 0 <= lo <= hi, 'invalid_calibration_offset')
        _require(doc.get('salt_curve') == 'additive_c_equals_w_plus_s', 'unknown_salt_curve')
        db.execute('INSERT INTO calibration_batch VALUES(?,?,?,?,?)',
                   (doc['batch_id'], float(lo), float(hi), doc['salt_curve'], doc['id']))
        return {'batch_id': doc['batch_id'], 'offset_min': float(lo), 'offset_max': float(hi)}


def import_probe(db_path, file):
    """Register a probe's frequency/temperature and its shared calibration batch."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'probe')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        plot = db.execute('SELECT id,area_m2 FROM nursery_plot WHERE id=?', (doc['plot_id'],)).fetchone()
        batch = db.execute('SELECT batch_id FROM calibration_batch WHERE batch_id=?',
                           (doc['batch_id'],)).fetchone()
        _require(plot is not None, 'probe_unknown_plot')
        _require(batch is not None, 'probe_unknown_calibration_batch')
        _require(isinstance(doc['frequency_hz'], (int, float)) and doc['frequency_hz'] > 0,
                 'invalid_probe_frequency')
        _require(isinstance(doc['temperature_c'], (int, float)), 'invalid_probe_temperature')
        _require(doc.get('salt_curve') == 'additive_c_equals_w_plus_s', 'unknown_salt_curve')
        _require(db.execute('SELECT COUNT(*) FROM probe WHERE probe_id=?', (doc['probe_id'],)).fetchone()[0] == 0,
                 'probe_id_conflict')
        db.execute('INSERT INTO probe VALUES(?,?,?,?,?,?,?)',
                   (doc['probe_id'], doc['plot_id'], float(doc['frequency_hz']),
                    float(doc['temperature_c']), doc['batch_id'], doc['salt_curve'], doc['id']))
        return {'probe_id': doc['probe_id'], 'plot_id': doc['plot_id'], 'batch_id': doc['batch_id']}


def import_tank(db_path, file):
    """Register a tank's remaining capacity and the adjacent plots it serves."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'water_tank')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        capacity, remaining = doc['capacity_liters'], doc['remaining_liters']
        _require(isinstance(capacity, (int, float)) and capacity > 0, 'invalid_tank_capacity')
        _require(isinstance(remaining, (int, float)) and 0 <= remaining <= capacity,
                 'invalid_tank_remaining')
        served = doc.get('served_plots')
        _require(isinstance(served, list) and served, 'tank_without_served_plots')
        for plot_id in served:
            _require(db.execute('SELECT 1 FROM nursery_plot WHERE id=?', (plot_id,)).fetchone() is not None,
                     'tank_unknown_plot')
        db.execute('INSERT INTO water_tank VALUES(?,?,?,?,?)',
                   (doc['tank_id'], float(capacity), float(remaining), json.dumps(served), doc['id']))
        return {'tank_id': doc['tank_id'], 'remaining_liters': float(remaining), 'served_plots': served}


def _check_batch_offset_consistency(db, cells):
    """D=W-S and C=W+S imply S=(C-D)/2; a batch must share one S offset."""
    by_batch = {}
    for cell in cells:
        if cell['response_d'] is None:
            continue
        reading = db.execute('SELECT capacitance FROM soil_reading WHERE source_id=?',
                             (cell['reading_source_id'],)).fetchone()
        probe = db.execute('SELECT batch_id FROM probe WHERE plot_id=?', (cell['plot_id'],)).fetchone()
        reading_plot = db.execute('SELECT plot_id FROM soil_reading WHERE source_id=?',
                                  (cell['reading_source_id'],)).fetchone()
        _require(reading is not None and probe is not None, 'scan_unknown_reading_or_probe')
        _require(reading_plot is not None and reading_plot['plot_id'] == cell['plot_id'],
                 'scan_reading_plot_mismatch')
        salt = (reading['capacitance'] - cell['response_d']) / 2.0
        by_batch.setdefault(probe['batch_id'], set()).add(round(salt, 9))
    for batch_id, offsets in by_batch.items():
        _require(len(offsets) == 1, f'shared_batch_offset_conflict:{batch_id}')
    return {batch_id: next(iter(offsets)) for batch_id, offsets in by_batch.items()}


def import_dual_frequency_scan(db_path, file):
    """Record a second-frequency sweep; shaded ridges stay explicitly unknown."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'dual_frequency_scan')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        cells = doc['cells']
        _require(isinstance(cells, list) and cells, 'scan_without_cells')
        seen = set()
        area_by_plot = {}
        for cell in cells:
            key = (cell['plot_id'], cell['cell_id'])
            _require(key not in seen, 'scan_duplicate_cell')
            seen.add(key)
            area = _bounds_area(cell, 'area_m2', 'bounds_m2')
            plot = db.execute('SELECT area_m2 FROM nursery_plot WHERE id=?', (cell['plot_id'],)).fetchone()
            _require(plot is not None, 'scan_unknown_plot')
            area_by_plot[cell['plot_id']] = area_by_plot.get(cell['plot_id'], 0.0) + area
            covered = bool(cell['covered'])
            if covered:
                _require(isinstance(cell['response_d'], (int, float)), 'scan_covered_without_response_d')
            else:
                _require(cell.get('response_d') is None, 'scan_shaded_cell_has_response')
        for plot_id, total in area_by_plot.items():
            plot_area = db.execute('SELECT area_m2 FROM nursery_plot WHERE id=?', (plot_id,)).fetchone()['area_m2']
            _require(total <= plot_area + 1e-9, 'scan_area_exceeds_plot')
            _require(db.execute('SELECT 1 FROM soil_sample WHERE plot_id=? LIMIT 1',
                                (plot_id,)).fetchone() is None, 'conflicting_evidence_routes')
        offsets = _check_batch_offset_consistency(db, [c for c in cells if c['covered']])
        db.execute('INSERT INTO dual_frequency_scan VALUES(?,?,?,?)',
                   (doc['id'], doc['scan_id'], doc['actor'], doc['observed_at']))
        for cell in cells:
            db.execute('INSERT INTO dual_frequency_cell VALUES(?,?,?,?,?,?,?)',
                       (doc['id'], cell['plot_id'], cell['reading_source_id'], cell['cell_id'],
                        _bounds_area(cell, 'area_m2', 'bounds_m2'),
                        cell['response_d'] if cell['covered'] else None, 1 if cell['covered'] else 0))
        return {'scan_id': doc['scan_id'], 'cells': len(cells),
                'shared_batch_offsets': offsets,
                'uncovered_area_m2': {
                    plot_id: round(db.execute('SELECT area_m2 FROM nursery_plot WHERE id=?',
                                              (plot_id,)).fetchone()['area_m2']
                                   - sum(c['area_m2'] for c in cells
                                         if c['plot_id'] == plot_id and c['covered']), 9)
                    for plot_id in area_by_plot}}


def import_soil_sample(db_path, file):
    """Record an independent oven-dry sample pinned to one bounded grid cell."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'soil_sample')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        plot = db.execute('SELECT area_m2 FROM nursery_plot WHERE id=?', (doc['plot_id'],)).fetchone()
        _require(plot is not None, 'sample_unknown_plot')
        area = _bounds_area(doc, 'area_m2', 'bounds_m2')
        _require(area <= plot['area_m2'] + 1e-9, 'sample_area_exceeds_plot')
        _require(isinstance(doc['water_pct'], (int, float)) and 0 <= doc['water_pct'] <= 100,
                 'invalid_sample_water_pct')
        _require(db.execute('SELECT 1 FROM soil_sample WHERE cell_id=?', (doc['cell_id'],)).fetchone() is None,
                 'sample_cell_already_recorded')
        _require(doc.get('method') == 'oven_dry', 'unknown_sample_method')
        # The two approved evidence routes are alternatives for a plot; a
        # dual-frequency sweep and an oven-dry sample must not be silently merged.
        _require(db.execute(
            """SELECT 1 FROM dual_frequency_cell WHERE plot_id=? LIMIT 1""",
            (doc['plot_id'],)).fetchone() is None, 'conflicting_evidence_routes')
        db.execute('INSERT INTO soil_sample VALUES(?,?,?,?,?,?,?,?,?)',
                   (doc['id'], doc['sample_id'], doc['plot_id'], doc['cell_id'], area,
                    json.dumps(doc['bounds_m2']), float(doc['water_pct']), doc['actor'], doc['observed_at']))
        return {'sample_id': doc['sample_id'], 'plot_id': doc['plot_id'],
                'cell_id': doc['cell_id'], 'water_pct': float(doc['water_pct']), 'area_m2': area}
