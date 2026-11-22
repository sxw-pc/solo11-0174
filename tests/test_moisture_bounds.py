"""Acceptance tests for saline-season moisture bounds and valve planning.

The single-frequency original is kept reproducible: C=50 still projects 0 L via
the legacy capacitance advice, while the saline planner keeps the full feasible
range (state A W=20/S=30 -> 400 L; state B W=40/S=10 -> 0 L) and never collapses
it to one chosen salt assumption.
"""
import json
from pathlib import Path

import pytest

from forestgate.soil import (import_plot, import_reading, import_calibration,
                             import_probe, import_tank, import_dual_frequency_scan,
                             import_soil_sample)
from forestgate.moisture_bounds import (water_content_bounds, solve_frequency_pair,
                                        water_need_bounds, build_plan,
                                        DRY_THRESHOLD, LITERS_PER_M2)
from forestgate.store import report, connect
from forestgate.audit import audit

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'saline-nursery.json'
IMPORTERS = {
    'nursery_plot': import_plot,
    'soil_reading': import_reading,
    'calibration_batch': import_calibration,
    'probe': import_probe,
    'water_tank': import_tank,
    'dual_frequency_scan': import_dual_frequency_scan,
    'soil_sample': import_soil_sample,
}


def load_bundle():
    return {doc['id']: doc for doc in json.loads(FIXTURE.read_text())['docs']}


def write(tmp_path, doc):
    file = tmp_path / f"{doc['id']}.json"
    file.write_text(json.dumps(doc))
    return file


def import_doc(db, tmp_path, docs, doc_id):
    doc = docs[doc_id]
    return IMPORTERS[doc['kind']](db, write(tmp_path, doc))


def import_baseline(db, tmp_path, docs, tank_id='T-600', exclude=()):
    """Plots, shared calibration, probes, readings, and one tank (no evidence)."""
    for doc_id in ('plot-P1', 'plot-P2', 'cal-CB1', 'probe-Q1', 'probe-Q2',
                   'reading-P1', 'reading-P2'):
        if doc_id not in exclude:
            import_doc(db, tmp_path, docs, doc_id)
    tank_doc = dict(docs[f'tank-{tank_id.replace("-", "")}'])
    import_tank(db, write(tmp_path, tank_doc))


def by_plot(plan):
    return {row['plot_id']: row for row in plan['plots']}


# --------------------------------------------------------------------------- 1
def test_baseline_capacitance_advice_remains_reproducible(tmp_path):
    """Acceptance 1: the original P1 C=50 -> 0 L projection still reproduces."""
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    import_doc(db, tmp_path, docs, 'plot-P1')
    advice = import_doc(db, tmp_path, docs, 'reading-P1')
    assert advice['liters'] == 0
    assert advice['reason'] == 'capacitance_ok'
    assert report(db)['irrigation_advice'][0]['reason'] == 'capacitance_ok'
    # Legacy projection is untouched even after saline plans are versioned in.
    import_doc(db, tmp_path, docs, 'cal-CB1')
    import_doc(db, tmp_path, docs, 'probe-Q1')
    tank = dict(docs['tank-T600'], served_plots=['P1'])
    import_tank(db, write(tmp_path, tank))
    build_plan(db, 'plan-P1', 'T-600')
    assert report(db)['irrigation_advice'] == [
        {'plot_id': 'P1', 'liters': 0, 'reason': 'capacitance_ok'}]


def test_pure_range_functions_keep_both_saline_worlds():
    # C=W+S, S in [10,30] -> W in [20,40] for C=50.
    assert water_content_bounds(50, 10, 30) == (20.0, 40.0)
    # State A: W=20, S=30 -> dry, 100 m2 * 4 L = 400 L minimum side.
    d_min, d_max = water_need_bounds(100, 20.0, 40.0)
    assert (d_min, d_max) == (0.0, 400.0)
    # Second frequency D=W-S: state A D=-10, state B D=30.
    assert solve_frequency_pair(50, -10) == (20.0, 30.0)
    assert solve_frequency_pair(50, 30) == (40.0, 10.0)
    # A reading already below threshold even at the wettest bound is certain.
    assert water_need_bounds(100, 5.0, 12.0) == (400.0, 400.0)
    assert water_need_bounds(100, 30.0, 45.0) == (0.0, 0.0)


# --------------------------------------------------------------------------- 2
def test_single_reading_keeps_range_and_both_states_before_evidence(tmp_path):
    """Acceptance 2: same original yields state A 400 L / state B 0 L; range first."""
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    import_baseline(db, tmp_path, docs)
    plan = build_plan(db, 'saline-plan', 'T-600')

    p1, p2 = by_plot(plan)['P1'], by_plot(plan)['P2']
    assert (p1['w_min'], p1['w_max']) == (20.0, 40.0)
    assert (p2['w_min'], p2['w_max']) == (20.0, 40.0)
    # Need is a range, never a single picked value: 0 L in the wet world,
    # 400 L in the dry world.
    assert (p1['demand_min_liters'], p1['demand_max_liters']) == (0.0, 400.0)
    assert (p2['demand_min_liters'], p2['demand_max_liters']) == (0.0, 400.0)

    # Joint states share one batch offset: only (S=10)->0 L and (S=30)->800 L
    # are possible; per-plot favourable offsets (400+0) must not appear.
    totals = sorted({s['total_demand_liters'] for s in plan['joint_states']})
    assert totals == [0.0, 800.0]
    for state in plan['joint_states']:
        offsets = set(state['offsets'].values())
        assert len(offsets) == 1  # shared batch offset, identical for P1 and P2
        if state['total_demand_liters'] == 800.0:
            assert state['plot_demands'] == {'P1': 400.0, 'P2': 400.0}
    # No plan row may pretend a unique water amount on ambiguous cells.
    assert p1['safe_liters'] == 0 and p2['safe_liters'] == 0
    assert plan['total_scheduled_liters'] == 0
    assert plan['status'] == 'measurement_required'
    valve = {(v['plot_id'], v['cell_id']): v for v in report(db)['valve_actions']}
    assert all(v['classification'] == 'needs_measurement' for v in valve.values())
    # The two saline worlds sit on identical single-frequency originals.
    rows = report(db)
    assert [r['capacitance'] for r in rows['soil_readings']] == [50.0, 50.0]


def test_no_fictitious_per_cell_optimum_under_shared_calibration(tmp_path):
    """Acceptance 3: independent per-plot favourable offsets are impossible."""
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    import_baseline(db, tmp_path, docs)
    plan = build_plan(db, 'shared-batch', 'T-600')
    # A fictitious 400+0 combination would total 400 L and fit the 600 L tank;
    # it must never appear because CB-1 has one shared offset.
    possible = sorted({s['total_demand_liters'] for s in plan['joint_states']})
    assert 400.0 not in possible
    # Every state is internally consistent: both plots share the same W value.
    for state in plan['joint_states']:
        shared_w = 50.0 - next(iter(state['offsets'].values()))
        assert all((50.0 - off) == shared_w for off in state['offsets'].values())
    # Tank feasibility flag distinguishes the dry world (800 > 600).
    fits = {s['total_demand_liters']: s['fits_tank'] for s in plan['joint_states']}
    assert fits[0.0] is True and fits[800.0] is False
    assert plan['tank_shortfall_liters'] == 200.0


def test_tank_shortfall_defers_plot_instead_of_silently_overusing(tmp_path):
    """When confirmed dry demand exceeds the tank, a plot awaits refill."""
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    # A dry-in-all-states batch (salt offset 0..5 -> W in [45,50]? no): make W
    # certainly dry with C=20, S in [0,5] -> W in [15,20] < 30 everywhere.
    dry_docs = json.loads(json.dumps(docs))
    dry_docs['cal-CB1']['offset_min'] = 0
    dry_docs['cal-CB1']['offset_max'] = 5
    for rid in ('reading-P1', 'reading-P2'):
        dry_docs[rid]['capacitance'] = 20
    import_baseline(db, tmp_path, dry_docs, tank_id='T-300')
    plan = build_plan(db, 'dry-plan', 'T-300')
    # Each plot certainly needs 400 L -> 800 L total against 300 L remaining.
    assert plan['max_state_demand_liters'] == 800.0
    assert plan['total_scheduled_liters'] == 300.0
    assert plan['tank_shortfall_liters'] == 500.0
    assert plan['status'] == 'awaiting_refill'
    rows = by_plot(plan)
    scheduled = {pid: r['scheduled_liters'] for pid, r in rows.items()}
    assert sum(scheduled.values()) == 300.0
    # Deterministic priority: lowest certain W, then plot id -> P1 first.
    # P1 takes the whole 300 L (still 100 L short), P2 can only await refill.
    assert scheduled['P1'] == 300.0 and scheduled['P2'] == 0.0
    assert rows['P1']['status'] == 'awaiting_refill'
    assert rows['P2']['status'] == 'awaiting_refill'
    valves = report(db)['valve_actions']
    assert any(v['classification'] == 'deferred_awaiting_refill' for v in valves)
    # Never overshoot: nothing scheduled beyond the 300 L actually available.
    assert sum(v['liters'] for v in valves) <= 300.0 + 1e-9


def test_refill_schedule_serves_one_full_plot_and_defers_the_other(tmp_path):
    """With exactly one plot's confirmed dose available, the other waits."""
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    dry_docs = json.loads(json.dumps(docs))
    dry_docs['cal-CB1']['offset_min'] = 0
    dry_docs['cal-CB1']['offset_max'] = 5
    for rid in ('reading-P1', 'reading-P2'):
        dry_docs[rid]['capacitance'] = 20
    import_baseline(db, tmp_path, dry_docs, tank_id='T-600')
    # Shrink the same tank to 400 L via a dedicated tank document, importing
    # the evidence without the standard tank first.
    fresh_db = tmp_path / 'fresh.sqlite'
    for doc_id in ('plot-P1', 'plot-P2', 'cal-CB1', 'probe-Q1', 'probe-Q2',
                   'reading-P1', 'reading-P2'):
        doc = dry_docs[doc_id]
        IMPORTERS[doc['kind']](fresh_db, write(tmp_path, doc))
    small_tank = {'kind': 'water_tank', 'id': 'tank-T400', 'tank_id': 'T-400',
                  'capacity_liters': 400, 'remaining_liters': 400,
                  'served_plots': ['P1', 'P2']}
    import_tank(fresh_db, write(tmp_path, small_tank))
    plan = build_plan(fresh_db, 'dry-plan', 'T-400')
    rows = by_plot(plan)
    assert rows['P1']['scheduled_liters'] == 400.0
    assert rows['P1']['status'] == 'confirmed'
    assert rows['P2']['scheduled_liters'] == 0.0
    assert rows['P2']['status'] == 'awaiting_refill'
    assert plan['total_scheduled_liters'] == 400.0
    assert plan['tank_shortfall_liters'] == 400.0


# --------------------------------------------------------------------------- 4a
def test_dual_frequency_scan_narrows_covered_cells_only(tmp_path):
    """Acceptance 4 route A: full-plot sweep solves covered cells, ridge unknown."""
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    import_baseline(db, tmp_path, docs)
    result = import_doc(db, tmp_path, docs, 'scan-full')
    # C=50, D=-10 -> S=30 shared across every covered cell of the batch.
    assert result['shared_batch_offsets'] == {'CB-1': 30.0}
    plan = build_plan(db, 'scan-plan', 'T-600')
    rows = report(db)
    cells = {(c['plot_id'], c['cell_id']): c for c in rows['plan_cells']}
    # Covered cells are point-known W=20 -> dry; shaded ridges stay ranges.
    for pid in ('P1', 'P2'):
        for cid in (f'{pid}-c1', f'{pid}-c2', f'{pid}-c3'):
            cell = cells[(pid, cid)]
            assert cell['coverage'] == 'point'
            assert cell['w_min'] == cell['w_max'] == 20.0
            assert cell['status'] == 'confirmed_dry'
        ridge = cells[(pid, f'{pid}-shaded-ridge')]
        assert ridge['coverage'] == 'range'
        assert (ridge['w_min'], ridge['w_max']) == (20.0, 40.0)
        assert ridge['status'] == 'needs_measurement'

    p1, p2 = by_plot(plan)['P1'], by_plot(plan)['P2']
    # Confirmed irrigation area is 75 m2 per plot -> 300 L each, fitting 600 L.
    assert p1['confirmed_area_m2'] == 75.0 and p1['pending_area_m2'] == 25.0
    assert p1['safe_liters'] == 300.0 and p1['scheduled_liters'] == 300.0
    assert p2['safe_liters'] == 300.0 and p2['scheduled_liters'] == 300.0
    assert plan['total_scheduled_liters'] == 600.0
    # Worst feasible state still dries the unknown ridges (800 L): residual
    # risk is recorded, never hidden by the confirmed dispatch.
    assert plan['tank_shortfall_liters'] == 200.0
    assert plan['status'] == 'measurement_required'
    safe = [v for v in rows['valve_actions'] if v['classification'] == 'safe_all_states']
    assert sum(v['liters'] for v in safe) == 600.0
    assert {v['cell_id'] for v in safe} == {f'P{i}-c{j}' for i in (1, 2) for j in (1, 2, 3)}
    # The narrowed range changes the plan but the original reading stays 50.
    assert [r['capacitance'] for r in report(db)['soil_readings']] == [50.0, 50.0]


def test_dual_frequency_scan_must_share_one_batch_offset(tmp_path):
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    import_baseline(db, tmp_path, docs)
    bad = json.loads(json.dumps(docs['scan-full']))
    bad['id'] = 'scan-conflict'
    # P2-c1 responding D=30 implies S=10 while every other cell implies S=30.
    for cell in bad['cells']:
        if cell['cell_id'] == 'P2-c1':
            cell['response_d'] = 30
    with pytest.raises(ValueError, match='shared_batch_offset_conflict'):
        import_dual_frequency_scan(db, write(tmp_path, bad))


def test_scan_cells_may_not_exceed_plot_boundary(tmp_path):
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    import_baseline(db, tmp_path, docs)
    bad = json.loads(json.dumps(docs['scan-full']))
    bad['id'] = 'scan-too-big'
    bad['cells'][0]['area_m2'] = 999
    with pytest.raises(ValueError, match='area_bounds_mismatch|scan_area_exceeds_plot'):
        import_dual_frequency_scan(db, write(tmp_path, bad))


# --------------------------------------------------------------------------- 4b
def test_oven_dry_sample_pins_one_cell_without_extrapolation(tmp_path):
    """Acceptance 4 route B: one 1 m2 grid cell confirmed, the rest unknown."""
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    import_baseline(db, tmp_path, docs)
    result = import_doc(db, tmp_path, docs, 'sample-P1-grid')
    assert result['water_pct'] == 20.0 and result['area_m2'] == 1.0
    plan = build_plan(db, 'sample-plan', 'T-600')
    rows = report(db)
    cells = {(c['plot_id'], c['cell_id']): c for c in rows['plan_cells']}

    sampled = cells[('P1', 'P1-grid-0-0')]
    assert sampled['coverage'] == 'point' and sampled['w_min'] == sampled['w_max'] == 20.0
    assert sampled['status'] == 'confirmed_dry' and sampled['safe_liters'] == LITERS_PER_M2
    # The other 99 m2 of P1 must NOT inherit the sampled 20 %.
    remainder = cells[('P1', 'unmeasured-remainder')]
    assert remainder['coverage'] == 'range'
    assert (remainder['w_min'], remainder['w_max']) == (20.0, 40.0)
    assert remainder['area_m2'] == 99.0 and remainder['status'] == 'needs_measurement'
    # P2 is wholly untouched by the P1 sample: whole-plot range, no point cell.
    p2_cells = [c for c in rows['plan_cells'] if c['plot_id'] == 'P2']
    assert len(p2_cells) == 1 and p2_cells[0]['coverage'] == 'range'

    p1, p2 = by_plot(plan)['P1'], by_plot(plan)['P2']
    assert p1['confirmed_area_m2'] == 1.0 and p1['pending_area_m2'] == 99.0
    assert p1['safe_liters'] == 4.0 and p1['scheduled_liters'] == 4.0
    assert p2['confirmed_area_m2'] == 0.0 and p2['pending_area_m2'] == 100.0
    assert p2['safe_liters'] == 0.0
    assert plan['total_scheduled_liters'] == 4.0
    assert plan['status'] == 'measurement_required'


def test_sample_area_is_boundary_checked_and_not_extrapolated(tmp_path):
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    import_baseline(db, tmp_path, docs)
    oversized = json.loads(json.dumps(docs['sample-P1-grid']))
    oversized['id'] = 'sample-big'
    oversized['sample_id'] = 'SM-big'
    oversized['cell_id'] = 'P1-grid-9-9'
    oversized['area_m2'] = 101
    oversized['bounds_m2'] = [[0, 0], [10.04987562112089, 0],
                              [10.04987562112089, 10.04987562112089],
                              [0, 10.04987562112089]]
    with pytest.raises(ValueError, match='sample_area_exceeds_plot'):
        import_soil_sample(db, write(tmp_path, oversized))
    # Bounds polygon area must agree with declared area.
    lying = json.loads(json.dumps(docs['sample-P1-grid']))
    lying['id'] = 'sample-lie'
    lying['sample_id'] = 'SM-lie'
    lying['cell_id'] = 'P1-grid-1-1'
    lying['area_m2'] = 2
    with pytest.raises(ValueError, match='area_bounds_mismatch'):
        import_soil_sample(db, write(tmp_path, lying))


def test_two_evidence_routes_cannot_be_silently_merged(tmp_path):
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    import_baseline(db, tmp_path, docs)
    import_doc(db, tmp_path, docs, 'scan-full')
    other_sample = json.loads(json.dumps(docs['sample-P1-grid']))
    other_sample['id'] = 'sample-after-scan'
    with pytest.raises(ValueError, match='conflicting_evidence_routes'):
        import_soil_sample(db, write(tmp_path, other_sample))


# --------------------------------------------------------------------------- 5
def test_plans_are_versioned_and_everything_is_traceable(tmp_path):
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    import_baseline(db, tmp_path, docs)
    first = build_plan(db, 'trace-plan', 'T-600')
    import_doc(db, tmp_path, docs, 'scan-full')
    second = build_plan(db, 'trace-plan', 'T-600')
    assert first['version'] == 1 and second['version'] == 2
    rows = report(db)
    assert [p['version'] for p in rows['moisture_plans']] == [1, 2]
    # Both plans keep their own projections; nothing is overwritten.
    assert len([p for p in rows['plan_plots'] if p['version'] == 1]) == 2
    assert len([p for p in rows['plan_plots'] if p['version'] == 2]) == 2
    v1 = next(p for p in rows['plan_plots'] if p['version'] == 1 and p['plot_id'] == 'P1')
    v2 = next(p for p in rows['plan_plots'] if p['version'] == 2 and p['plot_id'] == 'P1')
    assert v1['confirmed_area_m2'] == 0.0 and v2['confirmed_area_m2'] == 75.0
    # Evidence chain: plan references source ids of readings, probes, batch, tank, scan.
    evidence = set(json.loads(rows['moisture_plans'][1]['evidence_source_ids']))
    assert {'reading-P1', 'reading-P2', 'probe-Q1', 'probe-Q2', 'cal-CB1',
            'tank-T600', 'scan-full'} <= evidence
    # Original bytes survive and replay to the same evidence projections.
    replayed = audit(db)
    assert replayed['status'] == 'matched'
    assert replayed['source_count'] >= 8
    # Idempotent re-import of any original source is a replay, not a duplicate.
    again = import_doc(db, tmp_path, docs, 'reading-P1')
    assert again == {'id': 'reading-P1', 'replayed': True}
    assert len(report(db)['soil_readings']) == 2


def test_uncalibrated_plot_is_not_given_a_guessed_valve_amount(tmp_path):
    db = tmp_path / 'forest.sqlite'
    docs = load_bundle()
    # P1 only: no probe/calibration means no salt curve can bound W.
    import_doc(db, tmp_path, docs, 'plot-P1')
    import_doc(db, tmp_path, docs, 'reading-P1')
    tank = dict(docs['tank-T600'], served_plots=['P1'])
    import_tank(db, write(tmp_path, tank))
    plan = build_plan(db, 'raw-plan', 'T-600')
    p1 = by_plot(plan)['P1']
    assert p1['status'] == 'uncalibrated' and p1['w_min'] is None
    assert p1['safe_liters'] == 0 and p1['scheduled_liters'] == 0
    assert report(db)['valve_actions'][0]['classification'] == 'uncalibrated'
