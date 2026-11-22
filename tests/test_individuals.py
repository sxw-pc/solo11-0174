"""Acceptance: provable distinct-individual ranges across camera traps.

The same frames must never be collapsed into a fake single count. Local
track hints stay a provisional per-camera projection; confirmed identity
ranges are published as separate, append-only versions.
"""
import hashlib
import json
from pathlib import Path

import pytest

from forestgate.audit import audit
from forestgate.individuals import (import_clock, import_cosighting, import_gap,
                                    import_media, publish_assessment)
from forestgate.store import report
from forestgate.wildlife import import_camera, import_frame

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'deer-cross-camera.json'
IMPORTERS = {'wildlife_camera': import_camera, 'wildlife_frame': import_frame,
             'wildlife_clock': import_clock, 'wildlife_gap': import_gap}


def write(tmp_path, name, doc):
    file = tmp_path / f'{name}.json'
    file.write_text(json.dumps(doc), encoding='utf8')
    return file


def load_cross_camera_fixture(db, folder):
    for i, doc in enumerate(json.loads(FIXTURE.read_text(encoding='utf8'))):
        IMPORTERS[doc['kind']](db, write(folder, f'fixture-{i:02d}.json', doc))


def publish(db, tmp_path, doc_id, camera_ids, season='2026Q4', **extra):
    doc = {'kind': 'wildlife_assessment', 'id': doc_id, 'block_id': 'B1', 'species': 'deer',
           'season': season, 'camera_ids': camera_ids}
    doc.update(extra)
    return publish_assessment(db, write(tmp_path, f'{doc_id}.json', doc))


def media(doc_id, frame_id, shot_at, mark=None):
    return {'kind': 'wildlife_media', 'id': doc_id, 'frame_id': frame_id, 'media_kind': 'image',
            'sha': hashlib.sha256(f'raw-bytes-of-{doc_id}'.encode()).hexdigest(),
            'shot_at': shot_at, 'mark': mark, 'archive_ref': f'archive/raw/2026/{doc_id}.cr3'}


def camera(doc_id, camera_id, x_km, coverage=0.05):
    return {'kind': 'wildlife_camera', 'id': doc_id, 'camera_id': camera_id, 'block_id': 'B1',
            'x_km': x_km, 'y_km': 0, 'coverage_km': coverage}


def clock(doc_id, camera_id, session, lo_s, hi_s):
    return {'kind': 'wildlife_clock', 'id': doc_id, 'camera_id': camera_id, 'session': session,
            'offset_lo_s': lo_s, 'offset_hi_s': hi_s}


def frame(doc_id, camera_id, hint, at, session='boot-1'):
    return {'kind': 'wildlife_frame', 'id': doc_id, 'camera_id': camera_id, 'species': 'deer',
            'track_hint': hint, 'at': at, 'quality': 0.9, 'session': session}


def test_provisional_count_stays_a_track_hint_projection(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    # C1:T1 (two frames), C2:T1 and C3:T1: the local hints never merge across
    # cameras, so the provisional tally is a projection, not an individual count
    assert report(db)['wildlife_counts'] == [{'block_id': 'B1', 'species': 'deer', 'count': 3}]
    frames = report(db)['wildlife_frames']
    assert [f['source_id'] for f in frames] == ['frame-F1', 'frame-F2', 'frame-F3', 'frame-F4']


def test_same_observations_admit_one_and_two_individual_worlds(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    result = publish(db, tmp_path, 'assess-q4', ['C1', 'C2'])
    # 2 km in 10 minutes is reachable inside the shared clock slack, so the
    # frames admit exactly two physically complete worlds and the report is a
    # range with one full assignment per bound, not a single made-up value
    assert (result['lower'], result['upper']) == (1, 2)
    # one deer commutes C1 08:00 -> C2 08:10 -> C1 08:20
    assert result['lower_assignment'] == [['C1:T1', 'C2:T1']]
    # one deer stays at C1 while a second deer passes C2
    assert result['upper_assignment'] == [['C1:T1'], ['C2:T1']]


def test_remote_simultaneous_frame_cannot_be_merged_by_similarity(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    result = publish(db, tmp_path, 'assess-q4-wide', ['C1', 'C2', 'C3'])
    # all three tracks look alike, but C2 and C3 are 48 km apart at the same
    # minute: no deer is in both frames, so similarity must not assemble a
    # teleporting individual out of the three pairwise-similar tracks
    assert (result['lower'], result['upper']) == (2, 3)
    assert result['lower_assignment'] == [['C1:T1', 'C2:T1'], ['C3:T1']]
    for assignment in (result['lower_assignment'], result['upper_assignment']):
        assert not any('C2:T1' in individual and 'C3:T1' in individual for individual in assignment)


def test_shared_session_clock_excludes_per_frame_pseudo_path(tmp_path):
    def build(folder, c2_sessions):
        db = folder / 'forest.sqlite'
        import_camera(db, write(folder, 'cam1.json', camera('camera-C1', 'C1', 0)))
        import_camera(db, write(folder, 'cam2.json', camera('camera-C2', 'C2', 2)))
        import_clock(db, write(folder, 'clk1.json', clock('clock-C1', 'C1', 'boot-1', -60, 60)))
        for i, session in enumerate(dict.fromkeys(c2_sessions)):
            import_clock(db, write(folder, f'clk2-{i}.json', clock(f'clock-C2-{i}', 'C2', session, -60, 60)))
        import_frame(db, write(folder, 'fa.json', frame('frame-A', 'C1', 'T1', '2026-11-22T08:00:00Z')))
        import_frame(db, write(folder, 'fb1.json', frame('frame-B1', 'C2', 'T1', '2026-11-22T08:03:00Z', c2_sessions[0])))
        import_frame(db, write(folder, 'fb2.json', frame('frame-B2', 'C2', 'T1', '2026-11-22T08:05:00Z', c2_sessions[1])))
        import_frame(db, write(folder, 'fc.json', frame('frame-C', 'C1', 'T2', '2026-11-22T08:09:00Z')))
        return db

    # Both C2 frames share one power-on session, hence one offset: the
    # one-deer path A->B1->B2->C would need that offset >= +48s (A->B1) and
    # <= +12s (B2->C) at once, so the pseudo-path is excluded.
    shared_dir = tmp_path / 'shared'
    shared_dir.mkdir()
    db = build(shared_dir, ['boot-1', 'boot-1'])
    result = publish(db, shared_dir, 'assess-shared', ['C1', 'C2'])
    assert (result['lower'], result['upper']) == (2, 3)

    # Giving each image its own clock (the forbidden per-frame tuning) would
    # shift B1 by +60s and B2 by -60s and fake exactly the one-deer path the
    # shared session just rejected.
    per_frame_dir = tmp_path / 'per-frame'
    per_frame_dir.mkdir()
    db = build(per_frame_dir, ['boot-1', 'boot-2'])
    tuned = publish(db, per_frame_dir, 'assess-tuned', ['C1', 'C2'])
    assert (tuned['lower'], tuned['upper']) == (1, 3)


def test_merge_is_transitive_not_greedy_pairwise(tmp_path):
    db = tmp_path / 'forest.sqlite'
    import_camera(db, write(tmp_path, 'cam1.json', camera('camera-C1', 'C1', 0)))
    import_camera(db, write(tmp_path, 'cam2.json', camera('camera-C2', 'C2', 2)))
    import_frame(db, write(tmp_path, 'fa.json', frame('frame-A', 'C1', 'T1', '2026-11-22T08:00:00Z')))
    import_frame(db, write(tmp_path, 'fb.json', frame('frame-B', 'C2', 'T1', '2026-11-22T08:06:00Z')))
    import_frame(db, write(tmp_path, 'fc.json', frame('frame-C', 'C1', 'T2', '2026-11-22T08:08:00Z')))
    result = publish(db, tmp_path, 'assess-greedy', ['C1', 'C2'])
    # A~B (6 min for 1.9 km) and A~C (same camera) each look mergeable, so a
    # greedy pairwise union chains all three into one deer; but B cannot reach
    # C1 within 2 minutes, and merges must be transitive and globally
    # consistent, so the triple is infeasible.
    assert (result['lower'], result['upper']) == (2, 3)
    assert result['lower_assignment'] == [['C1:T1', 'C1:T2'], ['C2:T1']]


def test_broken_local_track_rejoins_across_camera_gap(tmp_path):
    db = tmp_path / 'forest.sqlite'
    import_camera(db, write(tmp_path, 'cam1.json', camera('camera-C1', 'C1', 0)))
    import_camera(db, write(tmp_path, 'cam2.json', camera('camera-C2', 'C2', 2)))
    import_gap(db, write(tmp_path, 'gap.json', {'kind': 'wildlife_gap', 'id': 'gap-C1', 'camera_id': 'C1',
                                                'start_at': '2026-11-22T08:05:00Z',
                                                'end_at': '2026-11-22T08:15:00Z'}))
    import_frame(db, write(tmp_path, 'fa.json', frame('frame-A', 'C1', 'T1', '2026-11-22T08:00:00Z')))
    import_frame(db, write(tmp_path, 'fb.json', frame('frame-B', 'C2', 'T1', '2026-11-22T08:10:00Z')))
    # the local tracker lost the deer during the outage and opened a new hint
    import_frame(db, write(tmp_path, 'fc.json', frame('frame-C', 'C1', 'T2', '2026-11-22T08:20:00Z')))
    result = publish(db, tmp_path, 'assess-broken', ['C1', 'C2'])
    # frames normalize into tracks first; the broken C1 track may still belong
    # to the deer that commuted C1 -> C2 -> C1 during the gap
    assert (result['lower'], result['upper']) == (1, 3)
    assert result['lower_assignment'] == [['C1:T1', 'C1:T2', 'C2:T1']]


def test_archived_raw_side_shots_shrink_upper_bound_without_rewriting_history(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    first = publish(db, tmp_path, 'assess-q4-v1', ['C1', 'C2'])
    assert (first['lower'], first['upper']) == (1, 2)
    notch = 'ear-notch:left-v-2cm'
    import_media(db, write(tmp_path, 'm1.json', media('media-F1', 'frame-F1', '2026-11-22T08:00:30Z', notch)))
    import_media(db, write(tmp_path, 'm2.json', media('media-F2', 'frame-F2', '2026-11-22T08:20:15Z', notch)))
    import_media(db, write(tmp_path, 'm3.json', media('media-F3', 'frame-F3', '2026-11-22T08:10:45Z', notch)))
    second = publish(db, tmp_path, 'assess-q4-v2', ['C1', 'C2'])
    # three archived side shots confirm the same ear notch and their source
    # times close with the frames: the C1/C2 tracks are provably one deer
    assert (second['lower'], second['upper'], second['version']) == (1, 1, 2)
    wide = publish(db, tmp_path, 'assess-q4-v3', ['C1', 'C2', 'C3'])
    # C3 has no side shot, so its track stays outside the merged individual
    assert (wide['lower'], wide['upper']) == (2, 2)
    assert wide['lower_assignment'] == [['C1:T1', 'C2:T1'], ['C3:T1']]
    assessments = report(db)['wildlife_assessments']
    assert [a['version'] for a in assessments] == [1, 2, 3]
    # the supplementary RAW import appended a new version; the old quarterly
    # report and the provisional track_hint tally were never rewritten
    assert (assessments[0]['lower_count'], assessments[0]['upper_count']) == (1, 2)
    assert report(db)['wildlife_counts'][0]['count'] == 3
    # original frames, media digests and per-version evidence stay traceable
    media_rows = report(db)['wildlife_media']
    assert [m['source_id'] for m in media_rows] == ['media-F1', 'media-F2', 'media-F3']
    assert all(len(m['sha']) == 64 and m['archive_ref'] for m in media_rows)
    evidence = json.loads(assessments[1]['evidence'])
    assert evidence['frames'] == ['frame-F1', 'frame-F2', 'frame-F3']
    assert evidence['media'] == ['media-F1', 'media-F2', 'media-F3']
    assert evidence['clocks'] == ['clock-C1-boot-1', 'clock-C2-boot-1']
    assert evidence['gaps'] == ['gap-C1-0803-0817']
    assert audit(db)['status'] == 'matched'


def test_marks_covering_only_c1_cannot_judge_unseen_c2(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    notch = 'ear-notch:left-v-2cm'
    import_media(db, write(tmp_path, 'm1.json', media('media-F1', 'frame-F1', '2026-11-22T08:00:30Z', notch)))
    import_media(db, write(tmp_path, 'm2.json', media('media-F2', 'frame-F2', '2026-11-22T08:20:15Z', notch)))
    result = publish(db, tmp_path, 'assess-partial', ['C1', 'C2'])
    # the notch evidence never saw C2's deer, so its identity stays a range
    assert (result['lower'], result['upper']) == (1, 2)


def test_conflicting_marks_prove_two_distinct_deer(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    import_media(db, write(tmp_path, 'm1.json', media('media-F1', 'frame-F1', '2026-11-22T08:00:30Z',
                                                      'ear-notch:left-v-2cm')))
    import_media(db, write(tmp_path, 'm3.json', media('media-F3', 'frame-F3', '2026-11-22T08:10:45Z',
                                                      'antler:6-point')))
    result = publish(db, tmp_path, 'assess-conflict', ['C1', 'C2'])
    # different confirmed marks: the similar-looking frames are two deer
    assert (result['lower'], result['upper']) == (2, 2)


def test_media_source_time_must_close_with_frame(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    with pytest.raises(ValueError, match='media_source_time_not_closed'):
        import_media(db, write(tmp_path, 'late.json',
                               media('media-late', 'frame-F1', '2026-11-22T10:00:00Z', 'ear-notch:left-v-2cm')))
    with pytest.raises(ValueError, match='unknown_frame'):
        import_media(db, write(tmp_path, 'ghost.json',
                               media('media-ghost', 'frame-F9', '2026-11-22T08:00:30Z')))


def test_frame_cannot_fall_inside_recorded_camera_gap(tmp_path):
    db = tmp_path / 'forest.sqlite'
    import_camera(db, write(tmp_path, 'cam.json', camera('camera-C1', 'C1', 0)))
    import_gap(db, write(tmp_path, 'gap.json', {'kind': 'wildlife_gap', 'id': 'gap-1', 'camera_id': 'C1',
                                                'start_at': '2026-11-22T08:05:00Z',
                                                'end_at': '2026-11-22T08:15:00Z'}))
    with pytest.raises(ValueError, match='frame_in_camera_gap'):
        import_frame(db, write(tmp_path, 'f.json', frame('frame-X', 'C1', 'T1', '2026-11-22T08:10:00Z')))


def test_synced_dual_camera_binds_new_season_without_backfilling_old_quarter(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    old = publish(db, tmp_path, 'assess-q4-v1', ['C1', 'C2'], season='2026Q4')
    assert (old['lower'], old['upper']) == (1, 2)
    # next sampling round: a synchronised pair with overlapping fields of view
    import_camera(db, write(tmp_path, 'camD1.json', camera('camera-D1', 'D1', 0)))
    import_camera(db, write(tmp_path, 'camD2.json', camera('camera-D2', 'D2', 0.06)))
    import_frame(db, write(tmp_path, 'f5.json', frame('frame-F5', 'D1', 'T1', '2026-12-05T08:00:00Z')))
    import_frame(db, write(tmp_path, 'f6.json', frame('frame-F6', 'D2', 'T1', '2026-12-05T08:00:00Z')))
    cosighting = {'kind': 'wildlife_cosighting', 'id': 'cosight-1',
                  'frame_ids': ['frame-F5', 'frame-F6'], 'cameras': ['D1', 'D2'],
                  'synced': True, 'overlap': True,
                  'valid_from': '2026-12-05T07:00:00Z', 'valid_to': '2026-12-05T09:00:00Z'}
    linked = import_cosighting(db, write(tmp_path, 'cosight.json', cosighting))
    assert linked['cameras'] == ['D1', 'D2']
    new = publish(db, tmp_path, 'assess-q1-v1', ['D1', 'D2'], season='2027Q1')
    # co-occurring in the shared overlap at the same synced instant: the new
    # season's pair is provably one individual across the two cameras
    assert (new['lower'], new['upper']) == (1, 1)
    # the new constraint cannot back-fill the old quarter's C1/C2 tracks
    again = publish(db, tmp_path, 'assess-q4-v2', ['C1', 'C2'], season='2026Q4')
    assert (again['lower'], again['upper']) == (1, 2)
    q4 = [a for a in report(db)['wildlife_assessments'] if a['season'] == '2026Q4']
    assert [(a['version'], a['lower_count'], a['upper_count']) for a in q4] == [(1, 1, 2), (2, 1, 2)]
    assert audit(db)['status'] == 'matched'


def test_cosighting_requires_sync_overlap_and_window(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    base = {'kind': 'wildlife_cosighting', 'id': 'cs-1', 'frame_ids': ['frame-F1', 'frame-F3'],
            'cameras': ['C1', 'C2'], 'synced': True, 'overlap': True,
            'valid_from': '2026-11-22T07:00:00Z', 'valid_to': '2026-11-22T09:00:00Z'}
    with pytest.raises(ValueError, match='invalid_cosighting'):
        import_cosighting(db, write(tmp_path, 'nosync.json', {**base, 'id': 'cs-2', 'synced': False}))
    with pytest.raises(ValueError, match='invalid_cosighting'):
        import_cosighting(db, write(tmp_path, 'outside.json',
                                    {**base, 'id': 'cs-3', 'valid_to': '2026-11-22T08:05:00Z'}))


def test_contradictory_cosighting_is_an_evidence_conflict(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    cosighting = {'kind': 'wildlife_cosighting', 'id': 'cs-remote',
                  'frame_ids': ['frame-F3', 'frame-F4'], 'cameras': ['C2', 'C3'],
                  'synced': True, 'overlap': True,
                  'valid_from': '2026-11-22T07:00:00Z', 'valid_to': '2026-11-22T09:00:00Z'}
    import_cosighting(db, write(tmp_path, 'cs.json', cosighting))
    # the claimed co-sighting would put one deer 48 km away at the same minute
    with pytest.raises(ValueError, match='wildlife_evidence_conflict'):
        publish(db, tmp_path, 'assess-bad', ['C1', 'C2', 'C3'])


def test_replayed_sources_do_not_double_count(tmp_path):
    db = tmp_path / 'forest.sqlite'
    load_cross_camera_fixture(db, tmp_path)
    replay = tmp_path / 'replay'
    replay.mkdir()
    load_cross_camera_fixture(db, replay)
    assert report(db)['wildlife_counts'][0]['count'] == 3
    assert len(report(db)['wildlife_frames']) == 4
