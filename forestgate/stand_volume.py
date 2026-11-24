"""Provable stand volume intervals under one shared flight elevation bias.

Raw lidar z values are surface observations relative to an unknown ground and
a whole-flight elevation bias.  Returns that never reached the ground cannot
prove where the ground is -- not even with every z read correctly and finer
canopy interpolation -- so this module publishes per-plot ground/canopy
combinations and volume intervals instead of point values:

* the flight datum declares a prior range for the one shared bias and a
  ground floor; the bias is a single parameter for the whole flight, never
  re-tuned per cell;
* ground control points (RTK) jointly pin that shared bias and fix the
  ground of their own plot -- they solve elevation only and cannot replace
  dbh for actual stem volume;
* stem plots measure dbh/height tree by tree and prove the volume and
  species of their own fixed-area cells only -- they cannot fabricate a
  stand-wide ground model, and their density is never extrapolated into
  unsurveyed area;
* plots without canopy returns (missed strips) stay unmeasured, never zero;
* stand totals are bounded only by complete scenarios (one shared bias plus
  a ground per plot), and both bounds ship with their witness scenarios;
* the legacy top-return estimate in timber.py is left untouched as the
  delivered harvest basis.
"""
import json
from contextlib import closing

from .store import connect, source

K = 0.02  # stand volume coefficient, identical to the legacy timber estimate
STEM_FORM_FACTOR = 0.4  # tree volume m3 = dbh_m**2 * height_m * STEM_FORM_FACTOR

_EPS = 1e-6


def _r3(x):
    return round(x, 3)


def _r6(x):
    return round(x, 6)


# ------------------------------------------------------------------ evidence

def import_datum(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'flight_datum')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _import_datum_doc(db, doc, doc['id'])


def _import_datum_doc(db, doc, source_id):
    if not doc.get('flight_id') or not doc.get('block_id') or not doc.get('datum') \
            or not doc.get('flown_at') or doc.get('ground_floor_m') is None \
            or doc.get('bias_prior_m') is None or doc.get('bias_tol_m', -1) < 0:
        raise ValueError('datum_fields')
    if db.execute('SELECT 1 FROM flight_datum WHERE block_id=? OR flight_id=?',
                  (doc['block_id'], doc['flight_id'])).fetchone():
        raise ValueError('datum_conflict')
    db.execute('INSERT INTO flight_datum VALUES(?,?,?,?,?,?,?,?)',
               (source_id, doc['flight_id'], doc['block_id'], doc['datum'],
                doc['ground_floor_m'], doc['bias_prior_m'], doc['bias_tol_m'], doc['flown_at']))
    reassess_for_block(db, doc['block_id'], source_id)
    return {'flight_id': doc['flight_id'], 'block_id': doc['block_id']}


def import_control(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'ground_control')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _import_control_doc(db, doc, doc['id'])


def _import_control_doc(db, doc, source_id):
    datum = db.execute('SELECT * FROM flight_datum WHERE flight_id=?', (doc.get('flight_id'),)).fetchone()
    if datum is None:
        raise ValueError('unknown_flight')
    plot = db.execute('SELECT * FROM lidar_plot WHERE id=?', (doc.get('plot_id'),)).fetchone()
    if plot is None or plot['block_id'] != datum['block_id']:
        raise ValueError('control_plot_unknown')
    if not doc.get('point_id') or doc.get('rtk_z_m') is None or doc.get('lidar_z_m') is None \
            or doc.get('tol_m', -1) < 0:
        raise ValueError('control_fields')
    if db.execute('SELECT 1 FROM ground_control WHERE point_id=?', (doc['point_id'],)).fetchone():
        raise ValueError('control_point_conflict')
    db.execute('INSERT INTO ground_control VALUES(?,?,?,?,?,?,?)',
               (source_id, doc['flight_id'], doc['point_id'], doc['plot_id'],
                doc['rtk_z_m'], doc['lidar_z_m'], doc['tol_m']))
    # joint check: one shared bias must satisfy every control point at once,
    # and pinned ground must sit below every observed return of that plot
    reassess_for_block(db, datum['block_id'], source_id)
    return {'point_id': doc['point_id'], 'plot_id': doc['plot_id']}


def import_stem_plot(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'stem_plot')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _import_stem_doc(db, doc, doc['id'])


def _import_stem_doc(db, doc, source_id):
    plot = db.execute('SELECT * FROM lidar_plot WHERE id=?', (doc.get('plot_id'),)).fetchone()
    if plot is None:
        raise ValueError('stem_plot_unknown')
    trees = doc.get('trees') or []
    if not trees:
        raise ValueError('stem_trees_required')
    if not doc.get('cell_id') or not doc.get('measured_at') or doc.get('area_m2', 0) <= 0:
        raise ValueError('stem_fields')
    if db.execute('SELECT 1 FROM stem_plot WHERE cell_id=?', (doc['cell_id'],)).fetchone():
        raise ValueError('stem_cell_conflict')
    used = db.execute('SELECT COALESCE(SUM(area_m2),0) FROM stem_plot WHERE plot_id=?',
                      (plot['id'],)).fetchone()[0]
    if used + doc['area_m2'] > plot['area_m2'] + _EPS:
        raise ValueError('stem_area_exceeded')
    seen, volumes = set(), []
    for tree in trees:
        if not isinstance(tree.get('tree_no'), int) or tree['tree_no'] < 1 or tree['tree_no'] in seen \
                or not tree.get('species') or tree.get('dbh_cm', 0) <= 0 or tree.get('height_m', 0) <= 0:
            raise ValueError('stem_tree_fields')
        seen.add(tree['tree_no'])
        volumes.append(_r3((tree['dbh_cm'] / 100) ** 2 * tree['height_m'] * STEM_FORM_FACTOR))
    db.execute('INSERT INTO stem_plot VALUES(?,?,?,?,?,?)',
               (source_id, doc['plot_id'], doc['cell_id'], doc['area_m2'],
                doc['measured_at'], _r3(sum(volumes))))
    for tree, volume in zip(trees, volumes):
        db.execute('INSERT INTO stem_tree VALUES(?,?,?,?,?,?)',
                   (source_id, tree['tree_no'], tree['species'], tree['dbh_cm'],
                    tree['height_m'], volume))
    reassess_for_block(db, plot['block_id'], source_id)
    return {'cell_id': doc['cell_id'], 'volume_m3': _r3(sum(volumes))}


def import_survey(db_path, file):
    """Import a bundled pre-harvest survey: flight datum, plots, raw returns
    and any supplementary evidence (control network and/or stem plots).  Every
    sub-document is registered as its own hashed source, so the raw returns,
    control points and stem surveys stay individually traceable."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'stand_survey')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        from .lidar import _import_plot_doc, _import_return_doc
        from .store import source_doc
        datum = doc.get('datum') or {}
        if datum.get('block_id') != doc.get('block_id'):
            raise ValueError('survey_datum')
        plots = doc.get('plots') or []
        returns = doc.get('returns') or []
        controls = doc.get('control_network') or []
        stems = doc.get('stem_plots') or []
        if any(p.get('block_id') != doc['block_id'] for p in plots):
            raise ValueError('survey_plot')
        for kind, docs, handler in (('flight_datum', [datum], _import_datum_doc),
                                    ('lidar_plot', plots, _import_plot_doc),
                                    ('lidar_return', returns, _import_return_doc),
                                    ('ground_control', controls, _import_control_doc),
                                    ('stem_plot', stems, _import_stem_doc)):
            for sub in docs:
                sub, sub_fresh = source_doc(db, sub, kind)
                if sub_fresh:
                    handler(db, sub, sub['id'])
        return {'block_id': doc['block_id'], 'plots': len(plots), 'returns': len(returns),
                'control_points': len(controls), 'stem_plots': len(stems)}


# --------------------------------------------------------------- assessment

def _bias_range(datum, controls):
    """The one shared flight bias, constrained jointly by datum and controls."""
    b_lo = datum['bias_prior_m'] - datum['bias_tol_m']
    b_hi = datum['bias_prior_m'] + datum['bias_tol_m']
    for ctl in controls:
        b_lo = max(b_lo, ctl['lidar_z_m'] - ctl['rtk_z_m'] - ctl['tol_m'])
        b_hi = min(b_hi, ctl['lidar_z_m'] - ctl['rtk_z_m'] + ctl['tol_m'])
    if b_lo > b_hi + _EPS:
        raise ValueError('control_bias_conflict')
    return b_lo, b_hi


def _stem_cells(db, plot_id):
    cells = []
    for stem in db.execute('SELECT * FROM stem_plot WHERE plot_id=? ORDER BY cell_id', (plot_id,)).fetchall():
        trees = db.execute('SELECT * FROM stem_tree WHERE stem_source_id=? ORDER BY tree_no',
                           (stem['source_id'],)).fetchall()
        cells.append({'cell_id': stem['cell_id'], 'plot_id': plot_id, 'area_m2': stem['area_m2'],
                      'volume_m3': _r3(sum(t['volume_m3'] for t in trees)),
                      'tree_count': len(trees),
                      'species': sorted({t['species'] for t in trees}),
                      'source_id': stem['source_id']})
    return cells


def _returns(db, plot_id):
    return [r['z_m'] for r in db.execute('SELECT z_m FROM lidar_return WHERE plot_id=?', (plot_id,)).fetchall()]


def _controls(db, flight_id):
    return db.execute('SELECT * FROM ground_control WHERE flight_id=? ORDER BY point_id', (flight_id,)).fetchall()


def assess_block(db, block_id):
    """Compute the current provable assessment for a block, or None when no
    flight datum is registered.  Raises on mutually inconsistent evidence."""
    datum = db.execute('SELECT * FROM flight_datum WHERE block_id=?', (block_id,)).fetchone()
    if datum is None:
        return None
    controls = _controls(db, datum['flight_id'])
    b_lo, b_hi = _bias_range(datum, controls)
    floor = datum['ground_floor_m']
    by_plot = {}
    for ctl in controls:
        by_plot.setdefault(ctl['plot_id'], []).append(ctl)
    plots, measured_cells, unmeasured = [], [], []
    measured_total = interval_lo = interval_hi = 0.0
    min_grounds, max_grounds = {}, {}
    for plot in db.execute('SELECT * FROM lidar_plot WHERE block_id=? ORDER BY id', (block_id,)).fetchall():
        pid = plot['id']
        cells = _stem_cells(db, pid)
        measured_cells.extend(cells)
        stem_area = sum(c['area_m2'] for c in cells)
        measured_total += sum(c['volume_m3'] for c in cells)
        remaining = plot['area_m2'] - stem_area
        if remaining < -_EPS:
            raise ValueError('stem_area_exceeded')
        zs = _returns(db, pid)
        if not zs:
            # missed strip: no canopy return, so volume is unknown, not zero
            if remaining > _EPS:
                unmeasured.append({'plot_id': pid, 'area_m2': _r6(remaining), 'reason': 'no_canopy_return'})
            continue
        if remaining <= _EPS:
            plots.append({'plot_id': pid, 'area_m2': plot['area_m2'], 'stem_area_m2': _r6(stem_area),
                          'remaining_area_m2': 0.0, 'status': 'measured'})
            continue
        z_lo, z_hi = min(zs), max(zs)
        pins = by_plot.get(pid, [])
        if pins:
            g_lo = max(c['rtk_z_m'] - c['tol_m'] for c in pins)
            g_hi = min(c['rtk_z_m'] + c['tol_m'] for c in pins)
            if g_lo > g_hi + _EPS or g_hi > z_lo - b_hi + _EPS:
                raise ValueError('control_inconsistent')
            g_lo = max(g_lo, floor)
            ground = {'basis': 'control_pinned', 'min_m': _r6(g_lo), 'max_m': _r6(g_hi),
                      'points': [c['point_id'] for c in pins]}
            h_min, h_max = z_hi - b_hi - g_hi, z_hi - b_lo - g_lo
            min_grounds[pid], max_grounds[pid] = _r6(g_hi), _r6(g_lo)
        else:
            if floor > z_lo - b_hi + _EPS:
                raise ValueError('return_below_ground')
            ground = {'basis': 'unproven', 'min_m': _r6(floor), 'max_m': _r6(z_lo - b_lo)}
            h_min, h_max = z_hi - z_lo, z_hi - b_lo - floor
            min_grounds[pid], max_grounds[pid] = _r6(z_lo - b_hi), _r6(floor)
        v_lo, v_hi = remaining * h_min * K, remaining * h_max * K
        interval_lo += v_lo
        interval_hi += v_hi
        plots.append({'plot_id': pid, 'area_m2': plot['area_m2'], 'stem_area_m2': _r6(stem_area),
                      'remaining_area_m2': _r6(remaining), 'return_count': len(zs),
                      'min_z_m': _r6(z_lo), 'max_z_m': _r6(z_hi), 'ground_m': ground,
                      'height_m': {'min': _r6(h_min), 'max': _r6(h_max)},
                      'volume_m3': {'min': _r3(v_lo), 'max': _r3(v_hi)},
                      'status': 'interval'})
    totals = {'measured_m3': _r3(measured_total),
              'interval_min_m3': _r3(interval_lo), 'interval_max_m3': _r3(interval_hi),
              'total_min_m3': _r3(measured_total + interval_lo),
              'total_max_m3': _r3(measured_total + interval_hi),
              'issuable_m3': _r3(measured_total + interval_lo),
              'unmeasured_area_m2': _r6(sum(x['area_m2'] for x in unmeasured)),
              'unmeasured_plots': [x['plot_id'] for x in unmeasured],
              'min_scenario': {'bias_m': _r6(b_hi), 'grounds': min_grounds},
              'max_scenario': {'bias_m': _r6(b_lo), 'grounds': max_grounds}}
    return {'block_id': block_id, 'flight_id': datum['flight_id'], 'datum': datum['datum'],
            'coefficient_m3_per_m2_m': K,
            'bias_m': {'min': _r6(b_lo), 'max': _r6(b_hi),
                       'basis': 'control_network' if controls else 'datum_prior'},
            'plots': plots, 'measured_cells': measured_cells, 'unmeasured': unmeasured,
            'totals': totals}


def reassess_for_block(db, block_id, source_id):
    """Append a new published assessment version after fresh evidence."""
    payload = assess_block(db, block_id)
    if payload is None:
        return None
    version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM stand_assessment WHERE block_id=?',
                         (block_id,)).fetchone()[0]
    totals = payload['totals']
    db.execute('INSERT INTO stand_assessment VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (block_id, version, source_id, payload['flight_id'],
                payload['bias_m']['min'], payload['bias_m']['max'],
                totals['measured_m3'], totals['total_min_m3'], totals['total_max_m3'],
                totals['unmeasured_area_m2'],
                json.dumps(payload, ensure_ascii=False, sort_keys=True)))
    return payload


def latest_assessment(db_path, block_id):
    with closing(connect(db_path)) as db:
        row = db.execute('SELECT payload FROM stand_assessment WHERE block_id=? '
                         'ORDER BY version DESC LIMIT 1', (block_id,)).fetchone()
        return json.loads(row['payload']) if row else None


def evaluate_scenario(db_path, block_id, bias_m, grounds):
    """Score one complete scenario: a single shared flight bias plus one
    ground per interval plot.  Anything the joint evidence excludes --
    per-cell zero points, a bias outside the shared range, ground above an
    observed return -- is rejected instead of scored."""
    with closing(connect(db_path)) as db:
        datum = db.execute('SELECT * FROM flight_datum WHERE block_id=?', (block_id,)).fetchone()
        if datum is None:
            raise ValueError('no_flight_datum')
        controls = _controls(db, datum['flight_id'])
        b_lo, b_hi = _bias_range(datum, controls)
        if not (b_lo - _EPS <= bias_m <= b_hi + _EPS):
            raise ValueError('scenario_infeasible')
        floor = datum['ground_floor_m']
        by_plot = {}
        for ctl in controls:
            by_plot.setdefault(ctl['plot_id'], []).append(ctl)
        out, interval_ids = {}, set()
        measured_total = interval_total = unmeasured_area = 0.0
        for plot in db.execute('SELECT * FROM lidar_plot WHERE block_id=? ORDER BY id', (block_id,)).fetchall():
            pid = plot['id']
            cells = _stem_cells(db, pid)
            remaining = plot['area_m2'] - sum(c['area_m2'] for c in cells)
            measured_total += sum(c['volume_m3'] for c in cells)
            zs = _returns(db, pid)
            if not zs:
                unmeasured_area += max(remaining, 0.0)
                continue
            if remaining <= _EPS:
                continue
            interval_ids.add(pid)
            if pid not in grounds:
                raise ValueError('scenario_infeasible')
            g = grounds[pid]
            pins = by_plot.get(pid, [])
            if pins:
                g_lo = max(max(c['rtk_z_m'] - c['tol_m'] for c in pins), floor)
                g_hi = min(c['rtk_z_m'] + c['tol_m'] for c in pins)
            else:
                g_lo, g_hi = floor, min(zs) - bias_m
            if not (g_lo - _EPS <= g <= g_hi + _EPS):
                raise ValueError('scenario_infeasible')
            height = max(zs) - bias_m - g
            volume = remaining * height * K
            out[pid] = {'ground_m': _r6(g), 'height_m': _r6(height), 'volume_m3': _r3(volume)}
            interval_total += volume
        if set(grounds) - interval_ids:
            raise ValueError('scenario_infeasible')
        return {'block_id': block_id, 'bias_m': _r6(bias_m), 'plots': out,
                'measured_m3': _r3(measured_total),
                'total_m3': _r3(measured_total + interval_total),
                'unmeasured_area_m2': _r6(unmeasured_area)}
