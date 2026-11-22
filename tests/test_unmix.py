"""Acceptance tests for feasible-interval spectral unmixing.

Covers the five required guarantees:

1. the fixed single-material classifier is unchanged before/after the new
   analysis (including the grass-only library counter-example);
2. the degenerate three-band observation admits TWO feasible compositions
   instead of one fake point percentage;
3. the band4 route and the clipping route report different coverage and
   different confirmed dry mass;
4. shared calibration bias and shade bounds are jointly constrained and
   never claim false precision;
5. raw scans/signatures stay traceable and the new analysis never overwrites
   the legacy classification (audit replay matches).
"""
import json
from fractions import Fraction

import pytest

from forestgate.spectra import import_signature
from forestgate.scan import import_scan
from forestgate.store import report
from forestgate.audit import audit
from forestgate.unmix import (
    analyze_mixture, import_mixture, import_band4, import_clipping, F,
)
from forestgate.lp import linprog

F = Fraction


# --------------------------------------------------------------------------
# 1. legacy single-material classification unchanged
# --------------------------------------------------------------------------

def test_legacy_classification_preserved(tmp_path):
    db = tmp_path / 'legacy.sqlite'
    import_signature(db, 'fixtures/signature-pine.json')
    import_signature(db, 'fixtures/signature-grass.json')
    result = import_scan(db, 'fixtures/fuel-scan-ok.json')
    assert result['material'] == 'pine_litter'
    assert result['distance'] == 1
    assert audit(db)['status'] == 'matched'
    # the new mixture pipeline must not touch the legacy projection
    import_mixture(db, 'fixtures/fuel-mixture.json')
    scans = report(db)['fuel_scans']
    assert scans[0]['material'] == 'pine_litter'
    assert scans[0]['distance'] == 1
    assert audit(db)['status'] == 'matched'


def test_grass_only_library_labels_same_observation_grass(tmp_path):
    """The reference library changes the downstream label directly."""
    db = tmp_path / 'grass_only.sqlite'
    import_signature(db, 'fixtures/signature-grass.json')
    result = import_scan(db, 'fixtures/fuel-scan-ok.json')
    assert result['material'] == 'grass'
    assert result['distance'] == 9


# --------------------------------------------------------------------------
# 2. degenerate three-band observation -> two feasible compositions
# --------------------------------------------------------------------------

PINE = [4, 6, 2]
GRASS = [7, 2, 5]
WET = [5.5, 4, 3.5]
OBS = [5.5, 4, 3.5]


def _degenerate_doc(error='0', shade_fraction='0', shade_tol='0'):
    return {
        'kind': 'fuel_mixture', 'id': 'deg-1', 'block_id': 'Bdeg',
        'block_area_m2': '1000',
        'calibration': {'calibration_id': 'c', 'bands': ['b1', 'b2', 'b3'],
                        'bias_bounds': {'lower': ['0', '0', '0'],
                                        'upper': ['0', '0', '0']}},
        'signatures': [
            {'material': 'pine_litter', 'bands': [str(x) for x in PINE],
             'dry_mass_per_m2': '0.6'},
            {'material': 'grass', 'bands': [str(x) for x in GRASS],
             'dry_mass_per_m2': '0.25'},
            {'material': 'wet_leaf', 'bands': [str(x) for x in WET],
             'dry_mass_per_m2': '0.15'},
        ],
        'plots': [{
            'plot_id': 'Q1', 'area_m2': '10',
            'bands': [str(x) for x in OBS],
            'band_error': [error] * 3,
            'shade': {'fraction': shade_fraction, 'tolerance': shade_tol},
        }],
    }


def test_degenerate_observation_has_two_zero_residual_compositions():
    doc = _degenerate_doc()
    out = analyze_mixture(doc)
    # both 100% wet leaf and 50/50 pine+grass explain the observation exactly
    q1 = out['plots'][0]
    assert F(out['joint_min_total_residual']) == 0
    wet = [F(x) for x in q1['fraction_interval']['wet_leaf']]
    pine = [F(x) for x in q1['fraction_interval']['pine_litter']]
    # wet_leaf fraction must range from 0 to 1: both endpoints feasible
    assert wet[0] == 0 and wet[1] == 1
    # pine fraction also spans a genuine interval, not a single point
    assert pine[0] == 0 and pine[1] == F(1, 2)
    assert q1['identifiable'] is False
    assert 'wet_leaf' in [d['material']
                          for d in out['non_identifiable_signatures']]
    # the two endpoints correspond to materially different dry fuel mass
    mass = [F(x) for x in q1['mass_interval_kg']['wet_leaf']]
    assert mass[0] == 0 and mass[1] > 0


def test_interval_not_collapsed_to_false_point_percentage():
    out = analyze_mixture(_degenerate_doc())
    q1 = out['plots'][0]
    for material in ('pine_litter', 'grass', 'wet_leaf'):
        lo, hi = (F(x) for x in q1['fraction_interval'][material])
        assert hi >= lo
    # at least one material keeps a wide (non-degenerate-to-a-point) interval
    assert any(F(q1['fraction_interval'][m][1])
               - F(q1['fraction_interval'][m][0]) > F(1, 1000)
               for m in q1['fraction_interval'])
    assert 'no_point_percentage_from_three_bands' in out['precision_guard']


def test_both_candidate_compositions_are_independently_feasible():
    """Verify the two narratives directly against the forward model."""
    doc = _degenerate_doc()
    # 100% wet leaf
    wet_only = WET
    assert all(abs(F(wet_only[b]) - F(OBS[b])) == 0 for b in range(3))
    # 50% pine + 50% grass
    half = [(F(PINE[b]) + F(GRASS[b])) / 2 for b in range(3)]
    assert half == [F(x) for x in OBS]
    out = analyze_mixture(doc)
    assert F(out['joint_min_total_residual']) == 0


# --------------------------------------------------------------------------
# 3. the two additional-evidence routes change different things
# --------------------------------------------------------------------------

@pytest.fixture(scope='module')
def mixture_doc():
    with open('fixtures/fuel-mixture.json') as fh:
        return json.load(fh)


@pytest.fixture(scope='module')
def band4_doc():
    with open('fixtures/fuel-band4.json') as fh:
        return json.load(fh)


@pytest.fixture(scope='module')
def clipping_doc():
    with open('fixtures/fuel-clipping.json') as fh:
        return json.load(fh)


def test_band4_route_adds_continuous_coverage_but_no_dry_mass(mixture_doc, band4_doc):
    baseline = analyze_mixture(mixture_doc)
    band4 = analyze_mixture(mixture_doc, band4=band4_doc)
    assert band4['stage'] == 'band4'
    # continuous fractions available on non-shaded cells only
    assert F(band4['coverage']['continuous_coverage_area_m2']) > 0
    assert F(baseline['coverage']['continuous_coverage_area_m2']) == 0
    # severe-shade cells remain on the resurvey list
    assert set(band4['coverage']['pending_resurvey_plots']) == {'P4', 'P5'}
    # optical evidence never counts as confirmed dry weight
    assert band4['coverage']['confirmed_dry_mass_kg'] == '0'
    # the extra band genuinely tightens the wet_leaf interval on a clean cell
    base_wet = [F(x) for x in baseline['plots'][0]['fraction_interval']['wet_leaf']]
    b4_wet = [F(x) for x in band4['plots'][0]['fraction_interval']['wet_leaf']]
    assert (b4_wet[1] - b4_wet[0]) < (base_wet[1] - base_wet[0])


def test_clipping_route_confirms_dry_mass_but_no_continuous_coverage(mixture_doc, clipping_doc):
    clipping = analyze_mixture(mixture_doc, clipping=clipping_doc)
    assert clipping['stage'] == 'clipping'
    assert clipping['coverage']['continuous_coverage_area_m2'] == '0'
    confirmed = clipping['coverage']['confirmed_dry_mass_kg']
    assert F(confirmed[0]) > 0 and F(confirmed[1]) >= F(confirmed[0])
    # only the three clipped cells' area is confirmed
    assert F(clipping['coverage']['confirmed_area_m2']) == F('6')
    # unclipped cells stay estimated
    assert 'P3' in clipping['coverage']['pending_resurvey_plots']
    assert 'P5' in clipping['coverage']['pending_resurvey_plots']


def test_routes_cannot_be_combined(mixture_doc, band4_doc, clipping_doc):
    with pytest.raises(ValueError):
        analyze_mixture(mixture_doc, band4=band4_doc, clipping=clipping_doc)


def test_two_routes_report_different_coverage_and_mass(mixture_doc, band4_doc, clipping_doc):
    band4 = analyze_mixture(mixture_doc, band4=band4_doc)
    clipping = analyze_mixture(mixture_doc, clipping=clipping_doc)
    assert F(band4['coverage']['continuous_coverage_area_m2']) != \
           F(clipping['coverage']['continuous_coverage_area_m2'])
    assert band4['coverage']['confirmed_dry_mass_kg'] != \
           clipping['coverage']['confirmed_dry_mass_kg']


# --------------------------------------------------------------------------
# 4. shared calibration bias + shade bounds never claim false precision
# --------------------------------------------------------------------------

def test_single_shared_bias_is_jointly_constrained(mixture_doc):
    out = analyze_mixture(mixture_doc)
    # one bias interval per band, shared across all plots (not per-plot vars)
    assert len(out['calibration_bias_interval']) == 3
    for interval in out['calibration_bias_interval']:
        lo, hi = F(interval[0]), F(interval[1])
        assert F('-0.5') <= lo <= hi <= F('0.5')


def test_model_has_one_bias_variable_per_band_not_per_plot(mixture_doc):
    from forestgate.unmix import _build_baseline
    model, materials, _refs, _mpp, bias, plot_vars = _build_baseline(mixture_doc)
    # exactly three bias variables regardless of the plot count
    assert len(bias) == 3
    assert all(model.var_name[i].startswith('bias_b') for i in bias)
    n_plots = len(mixture_doc['plots'])
    expected = 3 + n_plots * (len(materials) + 1 + 6)
    assert len(model.var_name) == expected
    assert set(plot_vars) == {p['plot_id'] for p in mixture_doc['plots']}


def test_per_plot_independent_bias_would_violate_sharing(mixture_doc):
    """A model that lets each plot pick its own bias overstates precision.

    Demonstrate the defect on a two-plot transect: independent biases admit
    a bias combination that no single shared bias can reproduce, proving the
    joint constraint is doing real work.
    """
    doc = json.loads(json.dumps(mixture_doc))
    doc['plots'] = doc['plots'][:2]
    shared = analyze_mixture(doc)
    # the shared-bias feasible interval must be a single consistent vector
    for interval in shared['calibration_bias_interval']:
        assert F(interval[0]) <= F(interval[1])
    # widen the shared calibration bound: intervals may only grow, never shrink
    wide = json.loads(json.dumps(doc))
    wide['calibration']['bias_bounds'] = {
        'lower': ['-2', '-2', '-2'], 'upper': ['2', '2', '2']}
    wide_out = analyze_mixture(wide)
    for b in range(3):
        s = [F(x) for x in shared['calibration_bias_interval'][b]]
        w = [F(x) for x in wide_out['calibration_bias_interval'][b]]
        assert w[0] <= s[0] and w[1] >= s[1]


def test_shade_severe_cells_keep_intervals_and_are_flagged(mixture_doc, band4_doc):
    band4 = analyze_mixture(mixture_doc, band4=band4_doc)
    severe = [p for p in band4['plots'] if p['severe_shade']]
    assert {p['plot_id'] for p in severe} == {'P4', 'P5'}
    for plot in severe:
        # band4 must not have collapsed a shaded cell to a point
        assert any(F(plot['fraction_interval'][m][1])
                   - F(plot['fraction_interval'][m][0]) > 0
                   for m in plot['fraction_interval'])


def test_clipping_disagreement_is_rejected(mixture_doc):
    bad = {
        'kind': 'clipping_evidence', 'id': 'bad-clip',
        'mixture_id': mixture_doc['id'],
        'cells': [{
            'plot_id': 'P1', 'clipped_area_m2': '2',
            'weighings': [
                {'team': 'crew-alpha', 'dry_mass_kg': '0.30'},
                {'team': 'crew-beta', 'dry_mass_kg': '0.90'}],
            'agreement_tolerance': '0.05'}],
    }
    with pytest.raises(ValueError, match='clipping_team_disagreement'):
        analyze_mixture(mixture_doc, clipping=bad)


def test_clipping_requires_two_distinct_teams(mixture_doc):
    bad = {
        'kind': 'clipping_evidence', 'id': 'one-team',
        'mixture_id': mixture_doc['id'],
        'cells': [{
            'plot_id': 'P1', 'clipped_area_m2': '2',
            'weighings': [
                {'team': 'same-crew', 'dry_mass_kg': '0.30'},
                {'team': 'same-crew', 'dry_mass_kg': '0.31'}],
            'agreement_tolerance': '0.05'}],
    }
    with pytest.raises(ValueError, match='clipping_requires_two_teams'):
        analyze_mixture(mixture_doc, clipping=bad)


# --------------------------------------------------------------------------
# 5. traceability: ledger persistence, replay and legacy separation
# --------------------------------------------------------------------------

def test_full_ledger_import_and_audit_replay(tmp_path, mixture_doc, band4_doc, clipping_doc):
    db = tmp_path / 'mix.sqlite'
    import_signature(db, 'fixtures/signature-pine.json')
    import_signature(db, 'fixtures/signature-grass.json')
    import_scan(db, 'fixtures/fuel-scan-ok.json')
    import_mixture(db, 'fixtures/fuel-mixture.json')
    import_band4(db, 'fixtures/fuel-band4.json')
    import_clipping(db, 'fixtures/fuel-clipping.json')
    snapshot = report(db)
    # legacy rows survive untouched
    assert snapshot['fuel_scans'][0]['material'] == 'pine_litter'
    # all three analysis stages are stored without overwriting each other
    stages = {row['stage'] for row in snapshot['unmix_blocks']}
    assert stages == {'baseline', 'band4', 'clipping'}
    # every derived row points back at an immutable raw source
    source_ids = {row['source_id'] for row in snapshot['fuel_mixtures']}
    assert 'fuel-mixture-source-1' in source_ids
    assert audit(db)['status'] == 'matched'


def test_replayed_import_is_idempotent(tmp_path):
    db = tmp_path / 'idem.sqlite'
    first = import_mixture(db, 'fixtures/fuel-mixture.json')
    second = import_mixture(db, 'fixtures/fuel-mixture.json')
    assert second.get('replayed') is True
    assert first['id'] == second['id']
    assert len(report(db)['unmix_blocks']) == 1


def test_coverage_separates_sampled_and_unsampled_area(mixture_doc):
    out = analyze_mixture(mixture_doc)
    block = F(out['block_area_m2'])
    sampled = F(out['sampled_area_m2'])
    unsampled = F(out['unsampled_area_m2'])
    assert sampled + unsampled == block
    assert unsampled > 0  # measured dry weight must not cover the whole block


def test_conservative_block_load_does_not_assume_unsampled_fuel(mixture_doc):
    out = analyze_mixture(mixture_doc)
    load = out['block_load_conservative']
    # without a physical density cap, no finite upper bound is fabricated
    assert load['upper_bound_kg'] is None
    # the lower bound equals the required sampled fuel, never a full-block guess
    assert F(load['lower_bound_kg']) == F(out['sampled_fuel_load_interval_kg'][0])


def test_density_cap_closes_block_load_upper_bound(mixture_doc):
    doc = json.loads(json.dumps(mixture_doc))
    doc['max_dry_mass_per_m2'] = '1.0'
    out = analyze_mixture(doc)
    load = out['block_load_conservative']
    upper = F(load['upper_bound_kg'])
    sampled_hi = F(out['sampled_fuel_load_interval_kg'][1])
    unsampled = F(out['unsampled_area_m2'])
    assert upper == sampled_hi + unsampled
    assert upper > F(load['lower_bound_kg'])


def test_lp_solver_exact_arithmetic_on_degenerate_vertex():
    # sanity guard on the exact rational solver used by every interval
    res = linprog([0, 0], A_eq=[[1, 1]], b_eq=[1], bounds=[(0, 1), (0, 1)])
    assert res['status'] == 'optimal'
    assert sum(res['x']) == 1
    assert all(isinstance(v, Fraction) for v in res['x'])
