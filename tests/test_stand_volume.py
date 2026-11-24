import json
from pathlib import Path

import pytest

from forestgate.audit import audit
from forestgate.lidar import import_plot, import_return
from forestgate.stand_volume import (evaluate_scenario, import_control, import_datum,
                                     import_stem_plot, import_survey, latest_assessment)
from forestgate.store import report

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'occluded-stand.json'
BUNDLE = json.loads(FIXTURE.read_text(encoding='utf8'))


def write(tmp_path, doc):
    file = tmp_path / f"{doc['id']}.json"
    file.write_text(json.dumps(doc), encoding='utf8')
    return file


def import_base(db, tmp_path):
    """Flight datum, plots and raw returns shared by every survey route."""
    import_datum(db, write(tmp_path, BUNDLE['datum']))
    for plot in BUNDLE['plots']:
        import_plot(db, write(tmp_path, plot))
    for ret in BUNDLE['returns']:
        import_return(db, write(tmp_path, ret))


def import_controls(db, tmp_path):
    for ctl in BUNDLE['control_network']:
        import_control(db, write(tmp_path, ctl))


def import_stems(db, tmp_path):
    for stem in BUNDLE['stem_plots']:
        import_stem_plot(db, write(tmp_path, stem))


def plots_by_id(assessment):
    return {p['plot_id']: p for p in assessment['plots']}


def test_legacy_top_return_value_survives_control_network(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    legacy = report(db)['timber_estimates']
    assert {x['plot_id']: (x['height_m'], x['volume_m3']) for x in legacy} == {'P1': (30, 60), 'P2': (28, 56)}
    import_controls(db, tmp_path)
    # the delivered harvest basis is not rewritten by later control points
    assert report(db)['timber_estimates'] == legacy
    assert report(db)['timber_estimates'][0]['volume_m3'] == 60
    assessment = latest_assessment(db, 'B1')
    p1 = plots_by_id(assessment)['P1']
    assert (p1['volume_m3']['min'], p1['volume_m3']['max']) == (35.6, 36.8)
    versions = report(db)['stand_assessments']
    assert len(versions) > 1 and versions[-1]['source_id'] == 'gcp-3'
    assert audit(db)['status'] == 'matched'


def test_single_top_return_admits_zero_and_ten_metre_grounds(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    p1 = plots_by_id(latest_assessment(db, 'B1'))['P1']
    assert p1['ground_m'] == {'basis': 'unproven', 'min_m': 0, 'max_m': 32}
    assert (p1['volume_m3']['min'], p1['volume_m3']['max']) == (0, 64)
    # identical raw return z=30 and identical area: ground 0 and ground 10 both fit
    zero = evaluate_scenario(db, 'B1', 0.0, {'P1': 0.0, 'P2': 0.0})
    ten = evaluate_scenario(db, 'B1', 0.0, {'P1': 10.0, 'P2': 0.0})
    assert zero['plots']['P1']['volume_m3'] == 60
    assert ten['plots']['P1']['height_m'] == 20
    assert ten['plots']['P1']['volume_m3'] == 40


def test_shared_bias_bounds_are_achievable_by_complete_scenarios(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    totals = latest_assessment(db, 'B1')['totals']
    assert (totals['total_min_m3'], totals['total_max_m3']) == (40, 124)
    # both bounds are realized by complete scenarios (one shared bias, all plots together)
    for key, expect in (('min_scenario', 40), ('max_scenario', 124)):
        scene = totals[key]
        assert evaluate_scenario(db, 'B1', scene['bias_m'], scene['grounds'])['total_m3'] == expect
    import_controls(db, tmp_path)
    # a +2 m shared flight bias shifts every plot's interpretation together
    shifted = evaluate_scenario(db, 'B1', 2.0, {'P1': 10.0, 'P2': 4.0})
    assert shifted['plots']['P1']['volume_m3'] == 36
    assert shifted['plots']['P2']['volume_m3'] == 44
    assert shifted['total_m3'] == 80
    # per-cell independent zero points are not a complete scenario: the joint
    # evidence excludes bias 0, and the naive per-cell total is unrealizable
    with pytest.raises(ValueError, match='scenario_infeasible'):
        evaluate_scenario(db, 'B1', 0.0, {'P1': 0.0, 'P2': 0.0})
    with pytest.raises(ValueError, match='scenario_infeasible'):
        evaluate_scenario(db, 'B1', 2.0, {'P1': 0.0, 'P2': 4.0})
    totals = latest_assessment(db, 'B1')['totals']
    assert (totals['total_min_m3'], totals['total_max_m3']) == (79.2, 81.6)
    naive_per_cell_total = 60 + 56  # each cell zeroed on its own, as the legacy method would
    assert naive_per_cell_total > totals['total_max_m3']
    for key, expect in (('min_scenario', 79.2), ('max_scenario', 81.6)):
        scene = totals[key]
        assert evaluate_scenario(db, 'B1', scene['bias_m'], scene['grounds'])['total_m3'] == expect


def test_control_network_narrows_intervals_but_missed_strip_is_not_zero(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    import_controls(db, tmp_path)
    assessment = latest_assessment(db, 'B1')
    assert assessment['bias_m'] == {'min': 1.8, 'max': 2.0, 'basis': 'control_network'}
    plots = plots_by_id(assessment)
    # full-coverage RTK pins the ground of every plot that still has a canopy top
    assert plots['P1']['ground_m'] == {'basis': 'control_pinned', 'min_m': 9.8, 'max_m': 10.2, 'points': ['GCP-1']}
    assert (plots['P1']['volume_m3']['min'], plots['P1']['volume_m3']['max']) == (35.6, 36.8)
    assert (plots['P2']['volume_m3']['min'], plots['P2']['volume_m3']['max']) == (43.6, 44.8)
    # the missed strip has a control point too, yet without a canopy return it
    # stays unmeasured -- never reported as zero volume
    assert assessment['unmeasured'] == [{'plot_id': 'P3', 'area_m2': 60, 'reason': 'no_canopy_return'}]
    totals = assessment['totals']
    assert (totals['total_min_m3'], totals['total_max_m3']) == (79.2, 81.6)
    assert totals['unmeasured_area_m2'] == 60
    assert totals['unmeasured_plots'] == ['P3']
    assert audit(db)['status'] == 'matched'


def test_stem_plots_verify_cells_without_fabricating_stand_ground(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    import_stems(db, tmp_path)
    assessment = latest_assessment(db, 'B1')
    cells = {c['cell_id']: c for c in assessment['measured_cells']}
    assert cells['S1']['volume_m3'] == 4.792
    assert cells['S2']['volume_m3'] == 4.608
    assert cells['S3']['volume_m3'] == 3.623
    assert cells['S3']['species'] == ['birch', 'spruce']
    totals = assessment['totals']
    assert totals['measured_m3'] == 13.023
    plots = plots_by_id(assessment)
    # only the 25 m2 of P1 outside the three cells keeps lidar bounds
    assert plots['P1']['remaining_area_m2'] == 25
    assert (plots['P1']['volume_m3']['min'], plots['P1']['volume_m3']['max']) == (0, 16)
    # unsurveyed plots keep their bounds: stem cells prove local volume only,
    # they cannot fabricate a stand-wide ground model or borrow density
    assert (plots['P2']['volume_m3']['min'], plots['P2']['volume_m3']['max']) == (40, 60)
    assert totals['unmeasured_area_m2'] == 60
    assert (totals['total_min_m3'], totals['total_max_m3']) == (53.023, 89.023)
    assert totals['issuable_m3'] == 53.023
    density_signoff = round(totals['measured_m3'] / 75 * 260, 3)
    assert totals['total_max_m3'] != density_signoff
    assert totals['issuable_m3'] != density_signoff
    assert audit(db)['status'] == 'matched'


def test_last_return_and_finer_canopy_cannot_fix_ground(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    plots = plots_by_id(latest_assessment(db, 'B1'))
    # the low z=8 return only bounds the ground from above; it is not declared
    # ground, because a slope or a low shrub may hold the last return
    assert plots['P2']['ground_m'] == {'basis': 'unproven', 'min_m': 0, 'max_m': 10}
    assert (plots['P2']['volume_m3']['min'], plots['P2']['volume_m3']['max']) == (40, 60)
    # return numbering carries no classification: swapping return_no changes nothing
    other = tmp_path / 'other.sqlite'
    import_datum(other, write(tmp_path, BUNDLE['datum']))
    import_plot(other, write(tmp_path, BUNDLE['plots'][1]))
    swapped = [dict(BUNDLE['returns'][1], id='ret-p2-l2a-sw', return_no=2),
               dict(BUNDLE['returns'][2], id='ret-p2-l2b-sw', return_no=1)]
    for ret in swapped:
        import_return(other, write(tmp_path, ret))
    assert plots_by_id(latest_assessment(other, 'B1'))['P2'] == plots['P2']
    # extra canopy returns refine the canopy, never the ground floor
    import_return(db, write(tmp_path, {'kind': 'lidar_return', 'id': 'ret-p1-l4',
                                       'plot_id': 'P1', 'beam_id': 'L4', 'z_m': 29.0, 'return_no': 1}))
    p1 = plots_by_id(latest_assessment(db, 'B1'))['P1']
    assert p1['volume_m3']['max'] == 64
    assert p1['volume_m3']['min'] == 2


def test_control_network_must_agree_on_one_shared_bias(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    import_control(db, write(tmp_path, BUNDLE['control_network'][0]))
    # a control point implying a different flight bias contradicts the network
    loner = {'kind': 'ground_control', 'id': 'gcp-x', 'flight_id': 'F1', 'point_id': 'GCP-X',
             'plot_id': 'P2', 'rtk_z_m': 4.0, 'lidar_z_m': 9.0, 'tol_m': 0.2}
    with pytest.raises(ValueError, match='control_bias_conflict'):
        import_control(db, write(tmp_path, loner))
    # ground pinned above an observed return is physically impossible
    sky = {'kind': 'ground_control', 'id': 'gcp-y', 'flight_id': 'F1', 'point_id': 'GCP-Y',
           'plot_id': 'P2', 'rtk_z_m': 9.0, 'lidar_z_m': 11.0, 'tol_m': 0.2}
    with pytest.raises(ValueError, match='control_inconsistent'):
        import_control(db, write(tmp_path, sky))
    # and a later return below already pinned ground is rejected as well
    import_control(db, write(tmp_path, BUNDLE['control_network'][1]))
    low = {'kind': 'lidar_return', 'id': 'ret-low', 'plot_id': 'P2', 'beam_id': 'L9',
           'z_m': 5.0, 'return_no': 1}
    with pytest.raises(ValueError, match='control_inconsistent'):
        import_return(db, write(tmp_path, low))
    assert len(report(db)['lidar_returns']) == 3
    assert len(report(db)['ground_controls']) == 2


def test_return_below_datum_floor_rejected(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    low = {'kind': 'lidar_return', 'id': 'ret-sub', 'plot_id': 'P1', 'beam_id': 'L8',
           'z_m': 1.0, 'return_no': 1}
    with pytest.raises(ValueError, match='return_below_ground'):
        import_return(db, write(tmp_path, low))


def test_one_flight_one_datum_per_block(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    other = dict(BUNDLE['datum'], id='datum-f2', flight_id='F2')
    with pytest.raises(ValueError, match='datum_conflict'):
        import_datum(db, write(tmp_path, other))
    ctl = dict(BUNDLE['control_network'][0], id='gcp-noflight', point_id='GCP-NF', flight_id='F9')
    with pytest.raises(ValueError, match='unknown_flight'):
        import_control(db, write(tmp_path, ctl))


def test_stem_plot_area_and_tree_contract(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    import_stems(db, tmp_path)
    too_big = {'kind': 'stem_plot', 'id': 'stem-big', 'plot_id': 'P1', 'cell_id': 'S9',
               'area_m2': 30, 'measured_at': '2026-10-21T10:00:00Z',
               'trees': [{'tree_no': 1, 'species': 'pine', 'dbh_cm': 40, 'height_m': 25.0}]}
    with pytest.raises(ValueError, match='stem_area_exceeded'):
        import_stem_plot(db, write(tmp_path, too_big))
    no_trees = {'kind': 'stem_plot', 'id': 'stem-empty', 'plot_id': 'P1', 'cell_id': 'S8',
                'area_m2': 25, 'measured_at': '2026-10-21T10:00:00Z', 'trees': []}
    with pytest.raises(ValueError, match='stem_trees_required'):
        import_stem_plot(db, write(tmp_path, no_trees))
    bad_tree = {'kind': 'stem_plot', 'id': 'stem-bad', 'plot_id': 'P1', 'cell_id': 'S7',
                'area_m2': 25, 'measured_at': '2026-10-21T10:00:00Z',
                'trees': [{'tree_no': 1, 'species': 'pine', 'dbh_cm': 0, 'height_m': 25.0}]}
    with pytest.raises(ValueError, match='stem_tree_fields'):
        import_stem_plot(db, write(tmp_path, bad_tree))
    assert len(report(db)['stem_plots']) == 3


def test_replayed_imports_do_not_add_versions(tmp_path):
    db = tmp_path / 'stand.sqlite'
    import_base(db, tmp_path)
    before = len(report(db)['stand_assessments'])
    again = import_return(db, write(tmp_path, BUNDLE['returns'][0]))
    assert again == {'id': 'ret-p1-l1', 'replayed': True}
    assert len(report(db)['stand_assessments']) == before


def test_survey_bundle_publishes_traceable_evidence(tmp_path):
    db = tmp_path / 'stand.sqlite'
    result = import_survey(db, FIXTURE)
    assert result == {'block_id': 'B1', 'plots': 3, 'returns': 3, 'control_points': 3, 'stem_plots': 3}
    book = report(db)
    # legacy report, raw returns, control points and supplementary surveys are all kept
    assert {x['plot_id']: (x['height_m'], x['volume_m3']) for x in book['timber_estimates']} == {'P1': (30, 60), 'P2': (28, 56)}
    assert len(book['lidar_returns']) == 3
    assert len(book['ground_controls']) == 3
    assert len(book['stem_trees']) == 5
    versions = book['stand_assessments']
    assert [v['version'] for v in versions] == list(range(1, len(versions) + 1))
    final = latest_assessment(db, 'B1')
    # both supplementary routes are visible in the published trail
    assert final['bias_m'] == {'min': 1.8, 'max': 2.0, 'basis': 'control_network'}
    assert final['totals']['measured_m3'] == 13.023
    assert (final['totals']['total_min_m3'], final['totals']['total_max_m3']) == (65.523, 67.023)
    assert final['totals']['unmeasured_plots'] == ['P3']
    assert audit(db)['status'] == 'matched'
