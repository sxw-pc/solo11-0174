"""Feasible moisture bounds and tank-constrained valve plans.

Single-frequency capacitance near a saline coast follows the synthetic
salt-sensitivity curve ``C = W + S`` where ``W`` is volumetric water percentage
and ``S`` the salt contribution.  One reading therefore cannot be reduced to a
single water content: every salt offset inside the calibration batch's shared
offset range is a feasible world, and a calibration batch's offset must satisfy
*all* its probes at once - per-plot offsets are not allowed.

The planner never picks one salt assumption.  It outputs:

1. the feasible ``[W_min, W_max]`` range and minimum/maximum irrigation need of
   every plot and cell;
2. the full set of joint feasible states (one shared offset per batch) and
   whether each state fits the shared tank;
3. valve actions that are safe in **every** feasible state, cells that need
   another measurement, and - when the tank cannot cover the safe demand -
   the plots that must wait for a refill. The plan never silently exceeds the
   tank.

Two approved evidence routes narrow the ranges differently and are not merged:

* a dual-frequency sweep adds ``D = W - S`` on covered cells, solving
  ``W = (C+D)/2`` there; shaded ridges stay unknown;
* an independent oven-dry sample fixes one bounded grid cell only and is never
  extrapolated to the rest of its plot or to another plot.
"""
import json
from contextlib import closing
from datetime import datetime, timezone

from .store import connect

DRY_THRESHOLD = 30.0          # W < 30 percent needs irrigation
LITERS_PER_M2 = 4.0           # four litres per square metre of dry soil
SALT_CURVE = 'additive_c_equals_w_plus_s'

# Cell/plan status vocabulary.
CONFIRMED_DRY = 'confirmed_dry'
CONFIRMED_WET = 'confirmed_wet'
NEEDS_MEASUREMENT = 'needs_measurement'
UNCALIBRATED = 'uncalibrated'
AWAITING_REFILL = 'awaiting_refill'
CONFIRMED = 'confirmed'

# Valve classification vocabulary.
VALVE_SAFE = 'safe_all_states'
VALVE_NO_WATER = 'no_water_required'
VALVE_MEASURE = 'needs_measurement'
VALVE_REFILL = 'deferred_awaiting_refill'
VALVE_UNCALIBRATED = 'uncalibrated'


def water_content_bounds(capacitance, offset_min, offset_max, salt_curve=SALT_CURVE):
    """Feasible volumetric water range for one single-frequency reading.

    With C = W + S and S in [offset_min, offset_max], W = C - S spans
    [C - offset_max, C - offset_min].
    """
    if salt_curve != SALT_CURVE:
        raise ValueError('unknown_salt_curve')
    if not 0 <= offset_min <= offset_max:
        raise ValueError('invalid_calibration_offset')
    return max(0.0, float(capacitance) - float(offset_max)), \
        float(capacitance) - float(offset_min)


def solve_frequency_pair(capacitance, response_d):
    """Recover (W, S) from C = W+S and D = W-S."""
    return ((float(capacitance) + float(response_d)) / 2.0,
            (float(capacitance) - float(response_d)) / 2.0)


def water_need_bounds(area_m2, w_min, w_max):
    """Minimum/maximum irrigation litres over a feasible water range.

    Dry in every feasible state (W_max < 30) -> the full dose is the minimum;
    wet in every feasible state (W_min >= 30) -> the maximum need is zero.
    """
    area = float(area_m2)
    minimum = area * LITERS_PER_M2 if w_max < DRY_THRESHOLD else 0.0
    maximum = area * LITERS_PER_M2 if w_min < DRY_THRESHOLD else 0.0
    return minimum, maximum


def _latest_reading(db, plot_id):
    return db.execute(
        'SELECT * FROM soil_reading WHERE plot_id=? ORDER BY at DESC, source_id DESC LIMIT 1',
        (plot_id,)).fetchone()


def _load_evidence(db, plot_id, area_m2, reading, batch):
    """Return (cells, evidence_source_ids) for one plot.

    A cell is a dict: cell_id, area_m2, coverage ('point'|'range'),
    w_min/w_max (None for uncalibrated), evidence_source_id.
    """
    scan_row = db.execute(
        """SELECT ds.source_id FROM dual_frequency_scan ds
           JOIN dual_frequency_cell dc ON dc.source_id=ds.source_id
           WHERE dc.plot_id=? ORDER BY ds.observed_at DESC LIMIT 1""",
        (plot_id,)).fetchone()
    samples = db.execute('SELECT * FROM soil_sample WHERE plot_id=? ORDER BY cell_id',
                         (plot_id,)).fetchall()
    if scan_row is not None and samples:
        raise ValueError('conflicting_evidence_routes')
    evidence = []
    cells = []

    if scan_row is not None:
        evidence.append(scan_row['source_id'])
        scan_cells = db.execute('SELECT * FROM dual_frequency_cell WHERE source_id=? AND plot_id=? ORDER BY cell_id',
                                (scan_row['source_id'], plot_id)).fetchall()
        covered_area = 0.0
        for sc in scan_cells:
            cell_reading = db.execute('SELECT capacitance FROM soil_reading WHERE source_id=?',
                                      (sc['reading_source_id'],)).fetchone()
            if cell_reading is None:
                raise ValueError('scan_unknown_reading_or_probe')
            capacitance = cell_reading['capacitance']
            if sc['covered']:
                w, salt = solve_frequency_pair(capacitance, sc['response_d'])
                if not (batch['offset_min'] - 1e-9 <= salt <= batch['offset_max'] + 1e-9):
                    raise ValueError('scan_offset_outside_batch')
                cells.append({'cell_id': sc['cell_id'], 'area_m2': sc['area_m2'],
                              'coverage': 'point', 'w_min': w, 'w_max': w,
                              'evidence_source_id': sc['source_id'],
                              'capacitance': capacitance})
                covered_area += sc['area_m2']
            else:
                w_min, w_max = water_content_bounds(
                    capacitance, batch['offset_min'], batch['offset_max'],
                    batch['salt_curve'])
                cells.append({'cell_id': sc['cell_id'], 'area_m2': sc['area_m2'],
                              'coverage': 'range', 'w_min': w_min, 'w_max': w_max,
                              'evidence_source_id': sc['source_id'],
                              'capacitance': capacitance})
        if abs(covered_area + sum(c['area_m2'] for c in cells if c['coverage'] == 'range')
               - area_m2) > 1e-6:
            raise ValueError('scan_area_exceeds_plot')
        return cells, evidence

    if samples:
        sampled_area = 0.0
        for sm in samples:
            evidence.append(sm['source_id'])
            cells.append({'cell_id': sm['cell_id'], 'area_m2': sm['area_m2'],
                          'coverage': 'point', 'w_min': sm['water_pct'], 'w_max': sm['water_pct'],
                          'evidence_source_id': sm['source_id'],
                          'capacitance': reading['capacitance'] if reading else None})
            sampled_area += sm['area_m2']
        if sampled_area > area_m2 + 1e-9:
            raise ValueError('sample_area_exceeds_plot')
        if batch is not None and reading is not None and area_m2 - sampled_area > 1e-9:
            w_min, w_max = water_content_bounds(
                reading['capacitance'], batch['offset_min'], batch['offset_max'],
                batch['salt_curve'])
            cells.append({'cell_id': 'unmeasured-remainder', 'area_m2': area_m2 - sampled_area,
                          'coverage': 'range', 'w_min': w_min, 'w_max': w_max,
                          'evidence_source_id': reading['source_id'],
                          'capacitance': reading['capacitance']})
        return cells, evidence

    if batch is None or reading is None:
        return [{'cell_id': 'whole-plot', 'area_m2': area_m2, 'coverage': 'uncalibrated',
                 'w_min': None, 'w_max': None, 'evidence_source_id': None,
                 'capacitance': reading['capacitance'] if reading else None}], evidence

    w_min, w_max = water_content_bounds(
        reading['capacitance'], batch['offset_min'], batch['offset_max'], batch['salt_curve'])
    return [{'cell_id': 'whole-plot', 'area_m2': area_m2, 'coverage': 'range',
             'w_min': w_min, 'w_max': w_max, 'evidence_source_id': reading['source_id'],
             'capacitance': reading['capacitance']}], evidence


def _enumerate_joint_states(plots, batch_by_id, range_cells, tank_remaining):
    """All corner joint states: one shared offset per calibration batch.

    A range cell flips dry/wet when S = C - 30, so the batch offset endpoints
    plus every in-range threshold offset exhaust the distinct demand patterns.
    """
    critical = {b: {batch_by_id[b]['offset_min'], batch_by_id[b]['offset_max']}
                for b in batch_by_id}
    for cell in range_cells:
        b = cell['batch_id']
        threshold = cell['capacitance'] - DRY_THRESHOLD
        lo, hi = batch_by_id[b]['offset_min'], batch_by_id[b]['offset_max']
        if lo < threshold < hi:
            critical[b].add(threshold)

    batch_ids = sorted(batch_by_id)
    choices = [sorted(critical[b]) for b in batch_ids]

    def product(idx, offsets):
        if idx == len(batch_ids):
            yield dict(offsets)
            return
        for value in choices[idx]:
            offsets[batch_ids[idx]] = value
            yield from product(idx + 1, offsets)

    states = []
    for seq, offsets in enumerate(product(0, {}), start=1):
        plot_demands = {}
        for plot in plots:
            demand = 0.0
            for cell in plot['cells']:
                if cell['coverage'] == 'point':
                    w = cell['w_min']
                elif cell['coverage'] == 'range':
                    w = cell['capacitance'] - offsets[cell['batch_id']]
                else:
                    w = None
                if w is not None and w < DRY_THRESHOLD:
                    demand += cell['area_m2'] * LITERS_PER_M2
            plot_demands[plot['plot_id']] = round(demand, 9)
        total = round(sum(plot_demands.values()), 9)
        states.append({'label': f'state_{seq}', 'offsets': {k: round(v, 9) for k, v in offsets.items()},
                       'plot_demands': plot_demands, 'total_demand_liters': total,
                       'fits_tank': total <= tank_remaining + 1e-9})
    return states


def build_plan(db_path, plan_id, tank_id=None):
    """Build and persist a versioned, tank-constrained irrigation plan."""
    with closing(connect(db_path)) as db, db:
        if tank_id is None:
            tanks = db.execute('SELECT tank_id FROM water_tank').fetchall()
            if len(tanks) != 1:
                raise ValueError('tank_id_required')
            tank_id = tanks[0]['tank_id']
        tank = db.execute('SELECT * FROM water_tank WHERE tank_id=?', (tank_id,)).fetchone()
        if tank is None:
            raise ValueError('unknown_tank')
        served = json.loads(tank['served_plots'])
        remaining = float(tank['remaining_liters'])

        plots = []
        batch_by_id = {}
        evidence_ids = {tank['source_id']}
        for plot_id in served:
            plot = db.execute('SELECT * FROM nursery_plot WHERE id=?', (plot_id,)).fetchone()
            if plot is None:
                raise ValueError('tank_unknown_plot')
            probe = db.execute('SELECT * FROM probe WHERE plot_id=? ORDER BY probe_id LIMIT 1',
                               (plot_id,)).fetchone()
            reading = _latest_reading(db, plot_id)
            if reading is not None:
                evidence_ids.add(reading['source_id'])
            batch = None
            if probe is not None:
                evidence_ids.add(probe['source_id'])
                batch = db.execute('SELECT * FROM calibration_batch WHERE batch_id=?',
                                   (probe['batch_id'],)).fetchone()
                if batch is None:
                    raise ValueError('probe_unknown_calibration_batch')
                evidence_ids.add(batch['source_id'])
                batch_by_id[batch['batch_id']] = dict(batch)
            cells, cell_evidence = _load_evidence(
                db, plot_id, plot['area_m2'], reading, batch)
            evidence_ids.update(cell_evidence)
            for cell in cells:
                cell['plot_id'] = plot_id
                cell['batch_id'] = probe['batch_id'] if probe is not None else None
            plots.append({'plot_id': plot_id, 'area_m2': plot['area_m2'], 'cells': cells})

        range_cells = [c for p in plots for c in p['cells'] if c['coverage'] == 'range']
        states = _enumerate_joint_states(plots, batch_by_id, range_cells, remaining)
        max_state_demand = max((s['total_demand_liters'] for s in states), default=0.0)

        # Per-cell safe water: the dose that is required in every feasible
        # state (the minimum need). Ambiguous cells dispatch nothing.
        all_cells = []
        for plot in plots:
            for cell in plot['cells']:
                if cell['coverage'] == 'uncalibrated':
                    cell['w_min_eff'] = cell['w_max_eff'] = None
                    cell['safe_liters'] = 0.0
                    cell['status'] = UNCALIBRATED
                else:
                    d_min, d_max = water_need_bounds(cell['area_m2'], cell['w_min'], cell['w_max'])
                    cell['demand_min'], cell['demand_max'] = d_min, d_max
                    cell['safe_liters'] = d_min
                    cell['status'] = (CONFIRMED_DRY if cell['w_max'] < DRY_THRESHOLD
                                      else CONFIRMED_WET if cell['w_min'] >= DRY_THRESHOLD
                                      else NEEDS_MEASUREMENT)
                all_cells.append(cell)

        # Allocate the shared tank greedily to drought-certain area first.
        rank = {CONFIRMED_DRY: 0, NEEDS_MEASUREMENT: 1, CONFIRMED_WET: 2, UNCALIBRATED: 3}
        ordered = sorted(all_cells, key=lambda c: (rank[c['status']],
                                                   c['w_max'] if c['w_max'] is not None else 1e9,
                                                   c['plot_id'], c['cell_id']))
        budget = remaining
        for cell in ordered:
            grant = min(cell['safe_liters'], max(0.0, budget))
            cell['scheduled_liters'] = round(grant, 9)
            budget -= grant
        total_scheduled = round(remaining - max(0.0, budget), 9)

        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM moisture_plan WHERE plan_id=?',
                             (plan_id,)).fetchone()[0]
        created_at = datetime.now(timezone.utc).isoformat()

        plot_rows = []
        valve_rows = []
        any_shortfall = False
        any_measure = any(c['status'] == NEEDS_MEASUREMENT for c in all_cells)
        any_uncalibrated = any(c['status'] == UNCALIBRATED for c in all_cells)

        for plot in plots:
            bounded = [c for c in plot['cells'] if c['w_min'] is not None]
            w_min = min((c['w_min'] for c in bounded), default=None)
            w_max = max((c['w_max'] for c in bounded), default=None)
            demand_min = round(sum(c.get('demand_min', 0.0) for c in plot['cells']), 9)
            demand_max = round(sum(c.get('demand_max', 0.0) for c in plot['cells']), 9)
            safe = round(sum(c['safe_liters'] for c in plot['cells']), 9)
            scheduled = round(sum(c['scheduled_liters'] for c in plot['cells']), 9)
            confirmed_area = round(sum(c['area_m2'] for c in plot['cells']
                                       if c['status'] in (CONFIRMED_DRY, CONFIRMED_WET)), 9)
            pending_area = round(sum(c['area_m2'] for c in plot['cells']
                                     if c['status'] in (NEEDS_MEASUREMENT, UNCALIBRATED)), 9)
            shortfall = round(safe - scheduled, 9)
            any_shortfall |= shortfall > 1e-9
            if any(c['status'] == UNCALIBRATED for c in plot['cells']):
                status = UNCALIBRATED
            elif shortfall > 1e-9:
                status = AWAITING_REFILL
            elif any(c['status'] == NEEDS_MEASUREMENT for c in plot['cells']):
                status = NEEDS_MEASUREMENT
            else:
                status = CONFIRMED
            plot_evidence = sorted({c['evidence_source_id'] for c in plot['cells']
                                    if c['evidence_source_id']})
            plot_rows.append((plan_id, version, plot['plot_id'], plot['area_m2'], w_min, w_max,
                              demand_min, demand_max, safe, scheduled, confirmed_area, pending_area,
                              status, json.dumps(plot_evidence)))

            for cell in sorted(plot['cells'], key=lambda c: c['cell_id']):
                db.execute('INSERT INTO plan_cell VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (plan_id, version, plot['plot_id'], cell['cell_id'], cell['area_m2'],
                            cell['coverage'], cell['w_min'], cell['w_max'],
                            cell['safe_liters'], cell['scheduled_liters'], cell['status'],
                            cell['evidence_source_id']))
                if cell['status'] == UNCALIBRATED:
                    classification, reason = VALVE_UNCALIBRATED, 'no_probe_or_reading'
                elif cell['status'] == NEEDS_MEASUREMENT:
                    classification, reason = VALVE_MEASURE, 'salt_offset_ambiguous'
                elif cell['scheduled_liters'] < cell['safe_liters'] - 1e-9:
                    classification, reason = VALVE_REFILL, 'tank_capacity_exceeded'
                elif cell['safe_liters'] == 0.0:
                    classification, reason = VALVE_NO_WATER, 'wet_in_all_feasible_states'
                else:
                    classification, reason = VALVE_SAFE, 'dry_in_all_feasible_states'
                valve_rows.append((plan_id, version, plot['plot_id'], cell['cell_id'],
                                   cell['scheduled_liters'], classification, reason))

        if any_uncalibrated:
            plan_status = UNCALIBRATED
        elif any_shortfall:
            plan_status = AWAITING_REFILL
        elif any_measure:
            plan_status = 'measurement_required'
        else:
            plan_status = CONFIRMED
        tank_shortfall = round(max(0.0, max_state_demand - remaining), 9)

        db.execute('INSERT INTO moisture_plan VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (plan_id, version, tank_id, remaining, total_scheduled, max_state_demand,
                    tank_shortfall, plan_status, json.dumps(sorted(evidence_ids)), created_at))
        db.executemany('INSERT INTO plan_plot VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', plot_rows)
        db.executemany('INSERT INTO valve_action VALUES(?,?,?,?,?,?,?)', valve_rows)
        for st in states:
            fits = st['total_demand_liters'] <= remaining + 1e-9
            seq = int(st['label'].split('_')[1])
            db.execute('INSERT INTO plan_joint_state VALUES(?,?,?,?,?,?,?,?)',
                       (plan_id, version, seq, st['label'], json.dumps(st['offsets']),
                        json.dumps(st['plot_demands']), st['total_demand_liters'], 1 if fits else 0))
        db.execute('INSERT INTO decision_log(system,object_id,status,reason,source_id) VALUES(?,?,?,?,?)',
                   ('moisture_plan', f'{plan_id}:v{version}', plan_status,
                    f'joint_states={len(states)};tank_remaining={remaining}', tank['source_id']))

    return {'plan_id': plan_id, 'version': version, 'status': plan_status,
            'tank_id': tank_id, 'tank_remaining_liters': remaining,
            'total_scheduled_liters': total_scheduled,
            'max_state_demand_liters': max_state_demand,
            'tank_shortfall_liters': tank_shortfall,
            'joint_states': states,
            'plots': [{'plot_id': r[2], 'w_min': r[4], 'w_max': r[5],
                       'demand_min_liters': r[6], 'demand_max_liters': r[7],
                       'safe_liters': r[8], 'scheduled_liters': r[9],
                       'confirmed_area_m2': r[10], 'pending_area_m2': r[11], 'status': r[12]}
                      for r in plot_rows]}
