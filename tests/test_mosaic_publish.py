"""Acceptance: deterministic mosaic build + crash-safe publish for the duty room.

These tests encode the fact that SQLite transactions cannot roll back the
filesystem: every fault point is exercised with a real process kill
(FORESTGATE_FAIL_AT -> os._exit) and the externally visible state is checked
before and after `mosaic-recover`.
"""
import hashlib, json, os, subprocess, sys, threading
from contextlib import closing
from pathlib import Path
import pytest
from forestgate.hotspot import import_tile
from forestgate.mosaic import build, publish, recover, verify, CrashInjected
from forestgate.store import connect, report
from forestgate.audit import audit

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / 'fixtures' / 'mosaic-two-tiles.json'
PARAMS = {'order': 'tile_id', 'columns': ['hot_pixels', 'cloud_pixels']}
TILE2 = {'kind': 'tile', 'id': 'tile-east-2', 'track_id': 'pass-1', 'block_hint': 'block-east',
         'captured_at': '2026-06-01T10:05:00Z', 'hot_pixels': 9, 'cloud_pixels': 1}
TILE3 = {'kind': 'tile', 'id': 'tile-east-3', 'track_id': 'pass-2', 'block_hint': 'block-east',
         'captured_at': '2026-06-02T10:05:00Z', 'hot_pixels': 4, 'cloud_pixels': 0}


def write(tmp, name, doc):
    p = tmp / name
    p.write_text(json.dumps(doc), encoding='utf8')
    return p


def req(doc_id, seq, tiles, publisher='duty-room-a'):
    return {'kind': 'mosaic', 'id': doc_id, 'block_id': 'block-east', 'seq': seq,
            'publisher': publisher, 'params': PARAMS, 'tiles': tiles}


def seeded(tmp, extra=(TILE2,)):
    db = tmp / 'mosaic.sqlite'
    assert import_tile(db, ROOT / 'fixtures' / 'tile-east.json')['status'] == 'captured'
    for t in extra:
        import_tile(db, write(tmp, f"tile-{t['id']}.json", t))
    return db


def current(db):
    rows = report(db)['mosaic_current']
    assert len(rows) <= 1  # mosaic_current is keyed by block: never two currents
    return rows[0] if rows else None


def artifact_of(out, matrix_sha):
    return out / 'published' / f'artifact-{matrix_sha}.mosaic'


def cli(env_extra, *args):
    env = {**os.environ, 'PYTHONPATH': str(ROOT), **env_extra}
    return subprocess.run([sys.executable, '-m', 'forestgate', *args],
                          capture_output=True, text=True, cwd=ROOT, env=env)


def test_build_then_publish_binds_one_version(tmp_path):
    db = seeded(tmp_path)
    out = tmp_path / 'mosaic'
    b = build(db, REQ, out)
    assert b['status'] == 'built' and not b['reused']
    # staging/candidate output is never treated as the current mosaic
    assert current(db) is None
    assert not (out / 'published').exists()
    p = publish(db, REQ, out)
    assert p['status'] == 'published' and p['seq'] == 1 and p['build_id'] == b['build_id']
    cur = current(db)
    assert cur['block_id'] == 'block-east' and cur['seq'] == 1 and cur['build_id'] == b['build_id']
    # artifact file, digest, tile byte digests, params, seq and publish record all agree
    artifact = artifact_of(out, cur['matrix_sha'])
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == cur['matrix_sha']
    body = json.loads(artifact.read_text())
    assert body['matrix'] == [[5, 2], [9, 1]] and body['tiles'] == ['tile-east-1', 'tile-east-2']
    manifest = json.loads((out / 'published' / f"manifest-{cur['manifest_sha']}.json").read_text())
    assert manifest['build_id'] == b['build_id'] and manifest['matrix_sha'] == cur['matrix_sha']
    assert [t['sha'] for t in manifest['tiles']] == json.loads(cur['tile_shas'])
    pubs = report(db)['mosaic_publishes']
    assert len(pubs) == 1 and pubs[0]['state'] == 'confirmed'
    assert pubs[0]['intent_id'] == 'mosaic-req-east-1' and pubs[0]['build_id'] == b['build_id']
    assert verify(db, out)['status'] == 'ok'
    assert audit(db)['status'] == 'matched'
    # acceptance 1: the tile-east baseline still holds on the same database
    tiles = report(db)['tiles']
    assert tiles[0]['hot_pixels'] == 5 and tiles[0]['cloud_pixels'] == 2
    assert import_tile(db, ROOT / 'fixtures' / 'tile-east.json')['replayed']


def test_rerun_same_request_is_replayed_and_build_reused(tmp_path):
    db = seeded(tmp_path)
    out = tmp_path / 'mosaic'
    b1 = build(db, REQ, out)
    assert build(db, REQ, out)['reused']
    assert publish(db, REQ, out)['status'] == 'published'
    again = publish(db, REQ, out)
    assert again['status'] == 'replayed' and again['build_id'] == b1['build_id']
    assert len(report(db)['mosaic_publishes']) == 1
    assert current(db)['build_id'] == b1['build_id']
    # determinism: a different request with the same tile set (any order) reuses the build
    rev = write(tmp_path, 'req-rev.json', req('mosaic-req-east-1r', 2, ['tile-east-2', 'tile-east-1']))
    b3 = build(db, rev, out)
    assert b3['build_id'] == b1['build_id'] and b3['reused']


def test_confirmed_seq_cannot_be_rewritten_by_same_name_rerun(tmp_path):
    db = seeded(tmp_path, extra=(TILE2, TILE3))
    out = tmp_path / 'mosaic'
    publish(db, REQ, out)
    cur1 = current(db)
    other = write(tmp_path, 'other.json',
                  req('mosaic-req-east-9', 1, ['tile-east-1', 'tile-east-2', 'tile-east-3'], 'duty-room-c'))
    r = publish(db, other, out)
    assert r['status'] == 'lost' and r['reason'] == 'seq_taken'
    assert r['winner']['build_id'] == cur1['build_id']
    assert current(db) == cur1
    assert verify(db, out)['status'] == 'ok'


@pytest.mark.parametrize('point', ['after_intent', 'after_stage', 'after_install',
                                   'before_confirm_commit', 'after_confirm_commit'])
def test_crash_at_fault_point_recovers_to_complete_current(tmp_path, point):
    db = seeded(tmp_path, extra=(TILE2, TILE3))
    out = tmp_path / 'mosaic'
    assert publish(db, REQ, out)['status'] == 'published'  # seq 1 signed off
    old = current(db)
    req2 = write(tmp_path, 'req2.json',
                 req('mosaic-req-east-2', 2, ['tile-east-1', 'tile-east-2', 'tile-east-3'], 'duty-room-b'))
    killed = cli({'FORESTGATE_FAIL_AT': point}, 'mosaic-publish',
                 '--db', str(db), '--file', str(req2), '--out', str(out))
    assert killed.returncode != 0, killed.stdout + killed.stderr
    mid = current(db)
    if point == 'after_confirm_commit':
        assert mid['seq'] == 2  # the confirm transaction was already durable
    else:
        assert mid == old  # the signed-off old version is still the only visible one
    rec = cli({}, 'mosaic-recover', '--db', str(db), '--out', str(out))
    assert rec.returncode == 0, rec.stderr
    actions = json.loads(rec.stdout)['actions']
    now = current(db)
    assert now['seq'] == 2 and now['build_id'] != old['build_id']
    if point == 'after_confirm_commit':
        assert actions == []
    else:
        assert actions == [{'intent_id': 'mosaic-req-east-2', 'seq': 2,
                            'action': 'confirmed', 'build_id': now['build_id']}]
    # current points only at a complete artifact whose digest matches
    artifact = artifact_of(out, now['matrix_sha'])
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == now['matrix_sha']
    assert json.loads(artifact.read_text())['matrix'] == [[5, 2], [9, 1], [4, 0]]
    assert verify(db, out)['status'] == 'ok'
    assert audit(db)['status'] == 'matched'


def test_in_process_crash_then_recover(tmp_path):
    db = seeded(tmp_path)
    out = tmp_path / 'mosaic'
    with pytest.raises(CrashInjected):
        publish(db, REQ, out, fail_at='after_install')
    assert current(db) is None  # installed but unconfirmed is not visible
    assert list((out / 'published').iterdir())
    rec = recover(db, out)
    assert rec['actions'][0]['action'] == 'confirmed'
    assert current(db)['seq'] == 1
    assert verify(db, out)['status'] == 'ok'


def test_recovery_abandons_intent_when_build_is_unrebuildable(tmp_path):
    db = seeded(tmp_path)
    out = tmp_path / 'mosaic'
    with pytest.raises(CrashInjected):
        publish(db, REQ, out, fail_at='after_intent')
    # the candidate cache is wiped AND the tile bytes the intent depends on
    # are destroyed, so the prepared intent can no longer be rebuilt
    build_id = report(db)['mosaic_publishes'][0]['build_id']
    for f in (out / 'builds' / build_id).iterdir():
        f.unlink()
    with closing(connect(db)) as db_conn, db_conn:
        db_conn.execute("UPDATE source SET raw=x'00' WHERE id='tile-east-2'")
    rec = recover(db, out)
    assert rec['actions'][0]['action'] == 'abandoned'
    assert current(db) is None  # nothing half-built becomes visible
    assert report(db)['mosaic_publishes'][0]['state'] == 'abandoned'
    assert not list((out / 'staging').glob('mosaic-req-east-1.*'))


def test_concurrent_publishers_same_seq_single_winner(tmp_path):
    db = seeded(tmp_path, extra=(TILE2, TILE3))
    out = tmp_path / 'mosaic'
    ra = write(tmp_path, 'ra.json', req('mosaic-req-a', 7, ['tile-east-1', 'tile-east-2'], 'pub-a'))
    rb = write(tmp_path, 'rb.json', req('mosaic-req-b', 7, ['tile-east-1', 'tile-east-2', 'tile-east-3'], 'pub-b'))
    barrier = threading.Barrier(2)
    results = {}

    def run(name, f):
        try:
            results[name] = publish(db, f, out, _sync=barrier.wait)
        except Exception as exc:  # pragma: no cover - failure detail for debugging
            results[name] = exc

    threads = [threading.Thread(target=run, args=('a', ra)), threading.Thread(target=run, args=('b', rb))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(r['status'] for r in results.values())
    assert statuses == ['lost', 'published'], results
    loser = next(r for r in results.values() if r['status'] == 'lost')
    winner = next(r for r in results.values() if r['status'] == 'published')
    assert loser['build_id'] != winner['build_id']
    # the loser reclaimed its own staging and can read the winning version
    assert f"builds/{loser['build_id']}" in loser['reclaimed']
    assert not (out / 'builds' / loser['build_id']).exists()
    assert loser['winner']['build_id'] == winner['build_id']
    cur = current(db)
    assert cur['seq'] == 7 and cur['build_id'] == winner['build_id']
    assert len(report(db)['mosaic_publishes']) == 1
    assert verify(db, out)['status'] == 'ok'


def test_tile_change_only_creates_new_candidate_old_version_untouched(tmp_path):
    db = seeded(tmp_path, extra=(TILE2, TILE3))
    out = tmp_path / 'mosaic'
    p1 = publish(db, REQ, out)
    cur1 = current(db)
    artifact1 = artifact_of(out, cur1['matrix_sha'])
    bytes1 = artifact1.read_bytes()
    # re-importing a changed tile under the same id is rejected and changes nothing
    evil = write(tmp_path, 'tile-evil.json',
                 {'kind': 'tile', 'id': 'tile-east-1', 'track_id': 'pass-1', 'block_hint': 'block-east',
                  'captured_at': '2026-06-01T10:00:00Z', 'hot_pixels': 99, 'cloud_pixels': 2})
    with pytest.raises(ValueError, match='source_id_conflict'):
        import_tile(db, evil)
    # a changed tile set only produces a new candidate build
    req2 = write(tmp_path, 'req2.json',
                 req('mosaic-req-east-2', 2, ['tile-east-1', 'tile-east-2', 'tile-east-3'], 'duty-room-b'))
    b2 = build(db, req2, out)
    assert b2['build_id'] != p1['build_id']
    assert current(db) == cur1  # the signed-off mosaic is not rewritten by a candidate
    assert artifact1.read_bytes() == bytes1
    p2 = publish(db, req2, out)
    assert p2['status'] == 'published'
    assert current(db)['seq'] == 2 and current(db)['build_id'] == b2['build_id']
    # the old artifact stays readable under its original digest
    assert artifact1.exists()
    assert hashlib.sha256(artifact1.read_bytes()).hexdigest() == cur1['matrix_sha']
    assert artifact1.read_bytes() == bytes1
    assert verify(db, out)['status'] == 'ok'
    assert audit(db)['status'] == 'matched'


def test_tampering_with_artifact_manifest_or_pointer_is_detected(tmp_path):
    db = seeded(tmp_path)
    out = tmp_path / 'mosaic'
    publish(db, REQ, out)
    cur = current(db)
    assert verify(db, out)['status'] == 'ok'
    pub_a = artifact_of(out, cur['matrix_sha'])
    pub_m = out / 'published' / f"manifest-{cur['manifest_sha']}.json"
    orig_a, orig_m = pub_a.read_bytes(), pub_m.read_bytes()
    # 1. artifact file rewritten on disk
    pub_a.write_bytes(b'forged')
    v = verify(db, out)
    assert v['status'] == 'tampered' and any(p.startswith('artifact:') for p in v['problems'])
    assert audit(db)['status'] == 'divergent' and 'mosaic' in audit(db)['differences']
    pub_a.write_bytes(orig_a)
    assert verify(db, out)['status'] == 'ok'
    # 2. manifest rewritten on disk
    pub_m.write_bytes(b'forged')
    v = verify(db, out)
    assert v['status'] == 'tampered' and any(p.startswith('manifest:') for p in v['problems'])
    pub_m.write_bytes(orig_m)
    assert verify(db, out)['status'] == 'ok'
    # 3. DB current pointer moved by hand (a tamperer switches FK checks off first)
    with closing(connect(db)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute("UPDATE mosaic_current SET build_id='forged' WHERE block_id='block-east'")
    v = verify(db, out)
    assert v['status'] == 'tampered' and any(p.startswith('current_pointer:') for p in v['problems'])
    assert audit(db)['status'] == 'divergent'
    with closing(connect(db)) as conn, conn:
        conn.execute("UPDATE mosaic_current SET build_id=? WHERE block_id='block-east'", (cur['build_id'],))
    assert verify(db, out)['status'] == 'ok'
    assert audit(db)['status'] == 'matched'


def test_unknown_tile_and_block_mismatch_rejected(tmp_path):
    db = tmp_path / 'm.sqlite'
    import_tile(db, ROOT / 'fixtures' / 'tile-east.json')
    out = tmp_path / 'mosaic'
    missing = write(tmp_path, 'missing.json', req('mosaic-req-missing', 1, ['tile-east-1', 'tile-nope']))
    with pytest.raises(ValueError, match='tile_unknown'):
        build(db, missing, out)
    west = write(tmp_path, 'tile-west.json',
                 {'kind': 'tile', 'id': 'tile-west-1', 'track_id': 'pass-9', 'block_hint': 'block-west',
                  'captured_at': '2026-06-01T10:00:00Z', 'hot_pixels': 1, 'cloud_pixels': 1})
    import_tile(db, west)
    mixed = write(tmp_path, 'mixed.json', req('mosaic-req-mixed', 1, ['tile-east-1', 'tile-west-1']))
    with pytest.raises(ValueError, match='tile_block_mismatch'):
        build(db, mixed, out)
    assert current(db) is None
    assert audit(db)['status'] == 'matched'


def test_public_cli_mosaic_flow(tmp_path):
    db = seeded(tmp_path)
    out = tmp_path / 'mosaic'
    r1 = cli({}, 'mosaic-build', '--db', str(db), '--file', str(REQ), '--out', str(out))
    assert r1.returncode == 0, r1.stderr
    assert json.loads(r1.stdout)['status'] == 'built'
    r2 = cli({}, 'mosaic-publish', '--db', str(db), '--file', str(REQ), '--out', str(out))
    assert r2.returncode == 0, r2.stderr
    assert json.loads(r2.stdout)['status'] == 'published'
    r3 = cli({}, 'mosaic-verify', '--db', str(db), '--out', str(out))
    assert r3.returncode == 0, r3.stderr
    assert json.loads(r3.stdout)['status'] == 'ok'
    r4 = cli({}, 'report', '--db', str(db))
    assert json.loads(r4.stdout)['mosaic_current'][0]['seq'] == 1
