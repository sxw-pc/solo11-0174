"""Physical counter-examples and evidence-ledger behaviour for tilted tanks.

The headline case: a 2 m x 1 m x 0.6 m rectangular tank, head dipstick
h = 0.3 m. The same reading is physically consistent with 800 L (bottom
falling away at +0.1 m/m) or 400 L (-0.1 m/m); the naive level conversion
answers a single false 600 L for both worlds.
"""
import json
from pathlib import Path

import pytest

from forestgate.dip_volume import (
    TankGeometry,
    gross_volume_l,
    extractable_volume_l,
    gross_range_l,
    extractable_range_l,
    dip_only_ranges_l,
    naive_level_volume_l,
    whole_litre_bounds,
)
from forestgate.fuel import (
    import_evidence_batch,
    import_tilt,
    import_level_transfer,
    import_dip,
)
from forestgate.mission import reserve_mission, corroborate_mission, available_now
from forestgate.store import report
from forestgate.audit import audit

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'tilted-depot.json'

GEO = TankGeometry(length_m=2.0, width_m=1.0, height_m=0.6,
                   dead_m=0.01, slope_min=-0.1, slope_max=0.1)


def write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding='utf-8')
    return path


# ---------------------------------------------------------------------------
# Pure coordinate integration: the two physically feasible worlds
# ---------------------------------------------------------------------------

def test_same_head_dip_gives_two_feasible_stocks_not_a_false_single_value():
    h = 0.3
    assert gross_volume_l(GEO, h, 0.0) == pytest.approx(600.0)
    assert gross_volume_l(GEO, h, +0.1) == pytest.approx(800.0)
    assert gross_volume_l(GEO, h, -0.1) == pytest.approx(400.0)
    # Both worlds stay inside the shell (far-end depths 0.5 and 0.1 m).
    assert naive_level_volume_l(GEO, h) == pytest.approx(600.0)


def test_naive_level_conversion_would_misjudge_an_800_litre_mission():
    # The conventional answer treats both worlds as 600 L and would refuse
    # FM1 in the 800 L world (or promise it in the 400 L world).
    assert gross_volume_l(GEO, 0.3, +0.1) >= 800 > naive_level_volume_l(GEO, 0.3)
    assert gross_volume_l(GEO, 0.3, -0.1) < 800


def test_dead_stock_is_subtracted_only_from_extractable_volume():
    assert extractable_volume_l(GEO, 0.3, 0.0) == pytest.approx(580.0)
    assert extractable_volume_l(GEO, 0.3, +0.1) == pytest.approx(580.0)
    assert extractable_volume_l(GEO, 0.3, -0.1) == pytest.approx(399.5)
    # Gross volumes still include the dead wedge.
    assert gross_volume_l(GEO, 0.3, -0.1) == pytest.approx(400.0)
    assert extractable_volume_l(GEO, 0.005, +0.1) == 0.0


def test_measurement_errors_widen_both_bounds_conservatively():
    gross_low, gross_high = gross_range_l(GEO, 0.3, 0.01, +0.1, 0.0)
    ext_low, ext_high = extractable_range_l(GEO, 0.3, 0.01, +0.1, 0.0)
    assert gross_low < 800.0 < gross_high
    assert ext_low < extractable_volume_l(GEO, 0.3, +0.1) <= ext_high
    low_i, high_i = whole_litre_bounds(ext_low, ext_high)
    assert isinstance(low_i, int) and isinstance(high_i, int)
    assert low_i <= ext_low and high_i >= ext_high


def test_dip_without_attitude_remains_a_range_over_the_grade_band():
    ranges = dip_only_ranges_l(GEO, 0.3, 0.0)
    assert ranges['gross_low_l'] == pytest.approx(400.0)
    assert ranges['gross_high_l'] == pytest.approx(800.0)
    assert ranges['extractable_low_l'] < ranges['extractable_high_l']
    with pytest.raises(ValueError):
        TankGeometry(length_m=0, width_m=1, height_m=1)


def test_dry_wedge_and_crown_clamp_are_integrated_piecewise():
    geo = TankGeometry(length_m=2.0, width_m=1.0, height_m=0.6,
                       dead_m=0.0, slope_min=-0.1, slope_max=0.1)
    # h=0.05, s=-0.1: liquid vanishes at x=0.5; triangular wedge = 12.5 L.
    assert gross_volume_l(geo, 0.05, -0.1) == pytest.approx(12.5)
    # h=0.55, s=+0.1: far end would reach 0.75 m but the 0.6 m crown clamps.
    expected = 1000.0 * (0.575 * 0.5 + 0.6 * 1.5)
    assert gross_volume_l(geo, 0.55, 0.1) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Declaration ledger still reproduces the original baseline
# ---------------------------------------------------------------------------

def _load_batch(tmp_path):
    db = tmp_path / 'tilted.sqlite'
    result = import_evidence_batch(db, FIXTURE)
    return db, result


def test_declaration_ledger_reproduces_1000_reserved_800_balance_200(tmp_path):
    db, result = _load_batch(tmp_path)
    assert result['batch_id'] == 'tilted-depot-batch-1'
    report_data = report(db)
    depot = report_data['fuel_depots'][0]
    mission = report_data['fuel_missions'][0]
    assert (depot['id'], depot['stock_l']) == ('FD1', 200)
    assert (mission['id'], mission['need_l'], mission['status']) == ('FM1', 800, 'reserved')
    assert audit(db)['status'] == 'matched'


def test_baseline_single_file_fixtures_still_match(tmp_path):
    from forestgate.fuel import register_depot
    root = Path(__file__).resolve().parents[1]
    db = tmp_path / 'base.sqlite'
    assert register_depot(db, root / 'fixtures' / 'fuel-depot-ok.json')['stock_l'] == 1000
    assert reserve_mission(db, root / 'fixtures' / 'fuel-mission-ok.json')['status'] == 'reserved'
    assert report(db)['fuel_depots'][0]['stock_l'] == 200
    assert audit(db)['status'] == 'matched'


def test_dip_without_attitude_cannot_guarantee_the_mission(tmp_path):
    db, _ = _load_batch(tmp_path)
    dip = report(db)['dip_corroborations'][0]
    assert dip['kind'] == 'dip_only'
    assert (dip['gross_low_l'], dip['gross_high_l']) == (pytest.approx(400.0), pytest.approx(800.0))
    # Explicit request still cannot turn one reading into a guarantee.
    file = write(tmp_path, 'corr.json', {
        'id': 'corr-dip-1', 'kind': 'mission_corroboration',
        'mission_id': 'FM1', 'evidence_kind': 'dip'})
    assert corroborate_mission(db, file)['status'] == 'physical_unverified'
    assert audit(db)['status'] == 'matched'


# ---------------------------------------------------------------------------
# Arrangement A: recover the same-instant two-end tilt record
# ---------------------------------------------------------------------------

def test_positive_tilt_record_verifies_historical_800_litre_commitment(tmp_path):
    db, _ = _load_batch(tmp_path)
    tilt = write(tmp_path, 'tilt-plus.json', {
        'id': 'tilt-plus-1', 'kind': 'tank_tilt', 'depot_id': 'FD1',
        'block_id': 'B1', 'epoch': 'legs-1',
        'observed_at': '2026-11-20T08:00:00Z',
        'slope': 0.1, 'slope_error_m': 0.0})
    outcome = import_tilt(db, tilt)
    assert outcome['revised_dips'][0]['gross_l'] == (800, 800)
    revision = report(db)['fuel_mission_revisions'][-1]
    assert (revision['mission_id'], revision['status'], revision['shortfall_l']) == \
           ('FM1', 'physical_verified', None)
    # The old reserved commitment is retained untouched.
    assert report(db)['fuel_missions'][0]['status'] == 'reserved'
    assert audit(db)['status'] == 'matched'


def test_negative_tilt_record_keeps_reservation_but_opens_400_litre_shortfall(tmp_path):
    db, _ = _load_batch(tmp_path)
    tilt = write(tmp_path, 'tilt-minus.json', {
        'id': 'tilt-minus-1', 'kind': 'tank_tilt', 'depot_id': 'FD1',
        'block_id': 'B1', 'epoch': 'legs-1',
        'observed_at': '2026-11-20T08:00:00Z',
        'slope': -0.1, 'slope_error_m': 0.0})
    outcome = import_tilt(db, tilt)
    assert outcome['revised_dips'][0]['gross_l'] == (400, 400)
    revision = report(db)['fuel_mission_revisions'][-1]
    assert (revision['status'], revision['shortfall_l']) == ('physical_shortage', 400)
    assert report(db)['fuel_missions'][0]['status'] == 'reserved'
    assert audit(db)['status'] == 'matched'


def test_late_tilt_only_adds_a_version_and_cannot_cross_leg_epochs(tmp_path):
    db, _ = _load_batch(tmp_path)
    # A newer leg attitude must not be back-applied to the old dip.
    later = write(tmp_path, 'tilt-later.json', {
        'id': 'tilt-later-1', 'kind': 'tank_tilt', 'depot_id': 'FD1',
        'block_id': 'B1', 'epoch': 'legs-2',
        'observed_at': '2026-11-21T08:00:00Z',
        'slope': 0.1, 'slope_error_m': 0.0})
    assert import_tilt(db, later)['revised_dips'] == []
    assert report(db)['dip_corroborations'][-1]['kind'] == 'dip_only'
    # The late import of the *old* attitude opens version 2 for DIP1 only.
    old = write(tmp_path, 'tilt-old.json', {
        'id': 'tilt-old-1', 'kind': 'tank_tilt', 'depot_id': 'FD1',
        'block_id': 'B1', 'epoch': 'legs-1',
        'observed_at': '2026-11-20T08:00:00Z',
        'slope': 0.1, 'slope_error_m': 0.0})
    import_tilt(db, old)
    versions = [c['version'] for c in report(db)['dip_corroborations'] if c['dip_id'] == 'DIP1']
    assert versions == [1, 2]
    assert report(db)['fuel_mission_revisions'][-1]['status'] == 'physical_verified'
    assert audit(db)['status'] == 'matched'


def test_conflicting_tilt_for_same_instant_is_rejected(tmp_path):
    db, _ = _load_batch(tmp_path)
    first = write(tmp_path, 'tilt-a.json', {
        'id': 'tilt-a', 'kind': 'tank_tilt', 'depot_id': 'FD1', 'block_id': 'B1',
        'epoch': 'legs-1', 'observed_at': '2026-11-20T08:00:00Z',
        'slope': 0.1, 'slope_error_m': 0.0})
    import_tilt(db, first)
    second = write(tmp_path, 'tilt-b.json', {
        'id': 'tilt-b', 'kind': 'tank_tilt', 'depot_id': 'FD1', 'block_id': 'B1',
        'epoch': 'legs-1', 'observed_at': '2026-11-20T08:00:00Z',
        'slope': -0.1, 'slope_error_m': 0.0})
    with pytest.raises(ValueError, match='tilt_conflict'):
        import_tilt(db, second)


def test_tilt_outside_approved_leg_band_is_rejected(tmp_path):
    db, _ = _load_batch(tmp_path)
    bogus = write(tmp_path, 'tilt-bogus.json', {
        'id': 'tilt-bogus', 'kind': 'tank_tilt', 'depot_id': 'FD1', 'block_id': 'B1',
        'epoch': 'legs-1', 'observed_at': '2026-11-20T08:00:00Z',
        'slope': 0.5, 'slope_error_m': 0.0})
    with pytest.raises(ValueError, match='tilt_outside_approved_band'):
        import_tilt(db, bogus)


def test_dips_in_one_epoch_share_the_attitude_record(tmp_path):
    db, _ = _load_batch(tmp_path)
    dip2 = write(tmp_path, 'dip2.json', {
        'id': 'dip2-src', 'kind': 'tank_dip', 'depot_id': 'FD1', 'block_id': 'B1',
        'dip_id': 'DIP2', 'epoch': 'legs-1',
        'observed_at': '2026-11-20T12:00:00Z',
        'head_depth_m': 0.3, 'depth_error_m': 0.0})
    import_dip(db, dip2)
    # Tilt imported after both readings: only the same-instant dip gets it.
    tilt = write(tmp_path, 'tilt.json', {
        'id': 'tilt-noon', 'kind': 'tank_tilt', 'depot_id': 'FD1', 'block_id': 'B1',
        'epoch': 'legs-1', 'observed_at': '2026-11-20T12:00:00Z',
        'slope': 0.1, 'slope_error_m': 0.0})
    import_tilt(db, tilt)
    latest = {}
    for c in report(db)['dip_corroborations']:
        latest[c['dip_id']] = c['kind']
    # DIP1 keeps its dip-only range (no matching attitude), DIP2 shares the
    # noon attitude record even though the tilt arrived after the reading.
    assert latest == {'DIP1': 'dip_only', 'DIP2': 'tilt'}
    assert audit(db)['status'] == 'matched'


# ---------------------------------------------------------------------------
# Arrangement B: level the tank and meter the transfer now
# ---------------------------------------------------------------------------

def test_levelled_transfer_cannot_backdate_the_historical_commitment(tmp_path):
    db, _ = _load_batch(tmp_path)
    transfer = write(tmp_path, 'transfer.json', {
        'id': 'transfer-1', 'kind': 'level_transfer', 'depot_id': 'FD1',
        'block_id': 'B1', 'epoch': 'legs-level-1',
        'observed_at': '2026-11-22T08:00:00Z',
        'extractable_l': 700, 'error_l': 0})
    import_level_transfer(db, transfer)
    corr = write(tmp_path, 'corr-transfer.json', {
        'id': 'corr-transfer-1', 'kind': 'mission_corroboration',
        'mission_id': 'FM1', 'evidence_kind': 'transfer',
        'transfer_source_id': 'transfer-1'})
    with pytest.raises(ValueError, match='transfer_predates_commitment'):
        corroborate_mission(db, corr)
    assert [r['status'] for r in report(db)['fuel_mission_revisions']] == []


def test_levelled_transfer_backs_only_new_missions_without_double_counting(tmp_path):
    db, _ = _load_batch(tmp_path)
    transfer = write(tmp_path, 'transfer.json', {
        'id': 'transfer-1', 'kind': 'level_transfer', 'depot_id': 'FD1',
        'block_id': 'B1', 'epoch': 'legs-level-1',
        'observed_at': '2026-11-22T08:00:00Z',
        'extractable_l': 500, 'error_l': 10})
    import_level_transfer(db, transfer)
    m2 = write(tmp_path, 'fm2.json', {
        'id': 'fm2-src', 'kind': 'fuel_mission', 'mission_id': 'FM2',
        'block_id': 'B1', 'depot_id': 'FD1', 'need_l': 300,
        'reserved_at': '2026-11-22T09:00:00Z'})
    # The declaration ledger only has 200 L left: it cannot reserve FM2, but
    # the physical transfer evidence may still back this new commitment.
    assert reserve_mission(db, m2)['status'] == 'short'
    corr2 = write(tmp_path, 'corr2.json', {
        'id': 'corr-2', 'kind': 'mission_corroboration',
        'mission_id': 'FM2', 'evidence_kind': 'transfer',
        'transfer_source_id': 'transfer-1'})
    # 490 L guaranteed low bound, 300 already promised to FM2.
    assert corroborate_mission(db, corr2)['status'] == 'physical_verified'
    m3 = write(tmp_path, 'fm3.json', {
        'id': 'fm3-src', 'kind': 'fuel_mission', 'mission_id': 'FM3',
        'block_id': 'B1', 'depot_id': 'FD1', 'need_l': 250,
        'reserved_at': '2026-11-22T10:00:00Z'})
    assert reserve_mission(db, m3)['status'] == 'short'
    corr3 = write(tmp_path, 'corr3.json', {
        'id': 'corr-3', 'kind': 'mission_corroboration',
        'mission_id': 'FM3', 'evidence_kind': 'transfer',
        'transfer_source_id': 'transfer-1'})
    # Free low bound = 490 - (300 + 250) = -60: the same litres cannot be
    # promised twice.
    revision = corroborate_mission(db, corr3)
    assert (revision['status'], revision['shortfall_l']) == ('physical_shortage', 60)
    view = available_now(db, 'FD1')
    assert view['transfer']['committed_after_l'] == 550
    assert view['transfer']['free_low_l'] == -60
    assert audit(db)['status'] == 'matched'


def test_two_arrangements_produce_different_task_outcomes(tmp_path):
    """Same head dip: tilt evidence vs. levelled transfer diverge."""
    outcomes = {}
    for label, slope in (('plus', 0.1), ('minus', -0.1)):
        db = tmp_path / f'{label}.sqlite'
        import_evidence_batch(db, FIXTURE)
        tilt = write(tmp_path, f'tilt-{label}.json', {
            'id': f'tilt-{label}', 'kind': 'tank_tilt', 'depot_id': 'FD1',
            'block_id': 'B1', 'epoch': 'legs-1',
            'observed_at': '2026-11-20T08:00:00Z',
            'slope': slope, 'slope_error_m': 0.0})
        import_tilt(db, tilt)
        revision = report(db)['fuel_mission_revisions'][-1]
        outcomes[label] = (revision['status'], revision['shortfall_l'])
    assert outcomes['plus'] == ('physical_verified', None)
    assert outcomes['minus'] == ('physical_shortage', 400)
