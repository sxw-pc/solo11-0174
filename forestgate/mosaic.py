"""Deterministic mosaic build and crash-safe publish for the duty room.

SQLite transactions cannot roll back the filesystem.  Every mosaic version
therefore commits through an explicit two-phase protocol instead of the
naive "os.replace plus one SQLite transaction":

* switch the DB pointer first, replace the file afterwards -> a crash in
  between leaves ``report`` pointing at a file that does not exist (or at
  old bytes under a new digest);
* replace a FIXED file name first, commit afterwards -> a crash in between
  leaves the old committed version pointing at new bytes (old digest, new
  content).

Two sound commit protocols exist for the same batch of tiles:

A. files-first -- write the content-addressed artifact and manifest as
   immutable files, fsync them, then flip the ``current`` pointer inside
   one DB transaction.  A crash between file install and DB commit leaves
   unreferenced immutable files with no DB trace: a directory scan can
   garbage-collect them, and a completed build is reusable only while its
   files survive that GC.

B. intent-first (IMPLEMENTED HERE) -- transaction 1 persists a ``prepared``
   publish intent (block_id, seq, build_id, intent_id); the artifact and
   manifest are then staged and installed by atomic rename; transaction 2
   re-hashes the installed files and only then confirms the intent and
   moves the visible pointer.  A crash after the install but before the
   confirm leaves a ``prepared`` intent plus installed/staged bytes, so
   ``mosaic-recover`` must retry the intent (rebuild from the source tiles,
   reinstall, confirm) or withdraw it (state ``abandoned``, staging
   removed).  Until recovery runs, both protocols keep the OLD version
   visible; they differ in disk footprint (B pins files through the intent,
   A leaves anonymous files), in recovery debt (B keeps an explicit todo
   list, A only a GC scan), and in how much completed build work can be
   reused (B keeps the candidate because the intent references it).

Fault-injection points and the externally visible state after a hard kill:

=====================  ====================================  ===============
point                  before recovery                     after recover
=====================  ====================================  ===============
after_intent           current=old, intent prepared        current=new
after_stage            current=old, staging tmp file       current=new
after_install          current=old, published file, intent current=new
before_confirm_commit  same as after_install (txn rollback) current=new
after_confirm_commit   current=new (durable)               current=new, no-op
=====================  ====================================  ===============

In every case ``report``'s ``mosaic_current`` only ever points at a
complete artifact whose digest was re-verified inside the confirm
transaction.  A confirmed (block_id, seq) can never be rewritten: the
primary key rejects the same-name rerun, files are content-addressed and
immutable, and changing any input tile simply produces a new build_id
candidate while the signed-off artifact stays readable under its original
digest.  Candidate files under ``builds/`` are a regenerable cache derived
from the immutable source tiles; only published artifacts are verified.
"""
import hashlib, json, os, shutil, sqlite3
from contextlib import closing
from pathlib import Path
from .store import connect, source

MATRIX_SCHEMA = 'mosaic-matrix/1'
MANIFEST_SCHEMA = 'mosaic-manifest/1'
BUILD_SCHEMA = 'mosaic-build/1'
DEFAULT_PARAMS = {'order': 'tile_id', 'columns': ['hot_pixels', 'cloud_pixels']}
FAIL_POINTS = ('after_intent', 'after_stage', 'after_install', 'before_confirm_commit', 'after_confirm_commit')


class CrashInjected(Exception):
    """Simulated hard process kill at a named fault point (tests only)."""


class MosaicUnrecoverable(Exception):
    """A prepared intent can no longer be rebuilt from the source tiles."""


def _canonical(obj):
    return (json.dumps(obj, sort_keys=True, separators=(',', ':')) + '\n').encode('utf8')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _fail(point, fail_at, hard):
    if fail_at == point:
        if hard:
            os._exit(1)  # hard kill: no cleanup, no rollback hooks (crash simulation)
        raise CrashInjected(point)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_immutable(path, data):
    """Write a content-addressed file once; an existing file must hold the same bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _sha(path.read_bytes()) == _sha(data):
            return 'kept'
        raise ValueError('mosaic_immutable_conflict')
    tmp = path.with_name(f'{path.name}.{os.getpid()}.part')
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)
    return 'written'


def _stage(path, data):
    """(Re)write a per-intent staging file; a partial leftover from a crash is replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and _sha(path.read_bytes()) == _sha(data):
        return
    tmp = path.with_name(f'{path.name}.{os.getpid()}.part')
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _install(staging, target, data):
    """Atomically rename staging into the content-addressed published location."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _sha(target.read_bytes()) == _sha(data):
            staging.unlink(missing_ok=True)
            _fsync_dir(target.parent)
            return 'kept'
        raise ValueError('mosaic_published_corrupt')
    os.replace(staging, target)
    _fsync_dir(target.parent)
    return 'installed'


def _load_request(db, file):
    doc, fresh = source(db, file, 'mosaic')
    block = doc.get('block_id')
    tiles = doc.get('tiles')
    if not isinstance(block, str) or not block or not isinstance(tiles, list) or not tiles \
            or not all(isinstance(t, str) for t in tiles):
        raise ValueError('mosaic_request_invalid')
    if len(set(tiles)) != len(tiles):
        raise ValueError('mosaic_tiles_duplicate')
    params = doc.get('params', DEFAULT_PARAMS)
    if params != DEFAULT_PARAMS:
        raise ValueError('mosaic_params_unsupported')
    return doc, block, list(tiles), params


def _publish_fields(doc):
    seq = doc.get('seq')
    publisher = doc.get('publisher')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1 \
            or not isinstance(publisher, str) or not publisher:
        raise ValueError('mosaic_publish_fields')
    return seq, publisher


def _tile_rows(db, block, tiles):
    found = {}
    for tid in tiles:
        row = db.execute(
            'SELECT t.hot_pixels, t.cloud_pixels, t.block_hint, s.sha FROM tile_capture t '
            'JOIN source s ON s.id = t.source_id WHERE t.source_id = ?', (tid,)).fetchone()
        if not row:
            raise ValueError('tile_unknown')
        if row['block_hint'] != block:
            raise ValueError('tile_block_mismatch')
        found[tid] = row
    ordered = sorted(tiles)
    matrix = [[found[t]['hot_pixels'], found[t]['cloud_pixels']] for t in ordered]
    shas = [found[t]['sha'] for t in ordered]
    return ordered, matrix, shas


def _render_artifact(block, params, ordered, matrix):
    return _canonical({'schema': MATRIX_SCHEMA, 'block_id': block, 'params': params,
                       'tiles': ordered, 'matrix': matrix})


def _build_digest(block, params_sha, ordered, tile_shas, matrix_sha):
    return _sha(_canonical({'schema': BUILD_SCHEMA, 'block_id': block, 'params_sha': params_sha,
                            'tiles': [{'id': i, 'sha': s} for i, s in zip(ordered, tile_shas)],
                            'matrix_sha': matrix_sha}))


def _render_manifest(build_id, block, params, params_sha, ordered, tile_shas, matrix_sha):
    return _canonical({'schema': MANIFEST_SCHEMA, 'build_id': build_id, 'block_id': block,
                       'params': params, 'params_sha': params_sha,
                       'tiles': [{'id': i, 'sha': s} for i, s in zip(ordered, tile_shas)],
                       'matrix_sha': matrix_sha, 'rows': len(ordered), 'cols': len(params['columns'])})


def _record_out(db, out):
    path = str(Path(out).resolve())
    row = db.execute("SELECT value FROM mosaic_meta WHERE key='out_dir'").fetchone()
    if row:
        if row['value'] != path:
            raise ValueError('mosaic_out_mismatch')
    else:
        db.execute("INSERT INTO mosaic_meta VALUES('out_dir', ?)", (path,))


def _ensure_build(db, out, block, tiles, params):
    """Create the deterministic candidate (artifact + manifest + build row); reuse if known."""
    ordered, matrix, tile_shas = _tile_rows(db, block, tiles)
    artifact = _render_artifact(block, params, ordered, matrix)
    matrix_sha = _sha(artifact)
    params_sha = _sha(_canonical(params))
    build_id = _build_digest(block, params_sha, ordered, tile_shas, matrix_sha)
    manifest = _render_manifest(build_id, block, params, params_sha, ordered, tile_shas, matrix_sha)
    manifest_sha = _sha(manifest)
    created = db.execute('SELECT 1 FROM mosaic_build WHERE build_id=?', (build_id,)).fetchone() is None
    _write_immutable(out / 'builds' / build_id / 'artifact.mosaic', artifact)
    _write_immutable(out / 'builds' / build_id / 'manifest.json', manifest)
    if created:
        db.execute('INSERT INTO mosaic_build(build_id,block_id,params,params_sha,tile_ids,tile_shas,'
                   'matrix_sha,manifest_sha,rows,cols) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (build_id, block, json.dumps(params, sort_keys=True), params_sha,
                    json.dumps(ordered), json.dumps(tile_shas), matrix_sha, manifest_sha,
                    len(ordered), len(params['columns'])))
    _record_out(db, out)
    info = {'build_id': build_id, 'block_id': block, 'params': params, 'params_sha': params_sha,
            'tile_ids': ordered, 'tile_shas': tile_shas, 'matrix_sha': matrix_sha,
            'manifest_sha': manifest_sha, 'rows': len(ordered), 'cols': len(params['columns'])}
    return info, created


def build(db_path, file, out_dir):
    """mosaic-build: deterministic candidate + provenance manifest, never duty-room visible."""
    out = Path(out_dir)
    with closing(connect(db_path)) as db:
        db.execute('BEGIN IMMEDIATE')
        with db:
            doc, block, tiles, params = _load_request(db, file)
            info, created = _ensure_build(db, out, block, tiles, params)
            return {'status': 'built', 'reused': not created, 'request_id': doc['id'],
                    'build_id': info['build_id'], 'matrix_sha': info['matrix_sha'],
                    'manifest_sha': info['manifest_sha']}


def _lost(db, out, block, seq, intent_id, info, row):
    """Another publisher holds (block, seq): reclaim our staging, report the winner."""
    winner = dict(row) if row else None
    if winner is None:
        found = db.execute('SELECT build_id,intent_id,state,publisher FROM mosaic_publish '
                           'WHERE block_id=? AND seq=?', (block, seq)).fetchone()
        winner = dict(found) if found else None
    reclaimed = []
    staging = out / 'staging'
    if staging.exists():
        for tmp in sorted(staging.glob(intent_id + '.*')):
            tmp.unlink()
            reclaimed.append(f'staging/{tmp.name}')
    if info is not None:
        used = db.execute('SELECT 1 FROM mosaic_publish WHERE build_id=? LIMIT 1',
                          (info['build_id'],)).fetchone()
        if not used:
            # our candidate is referenced by no intent: reclaim it (it is regenerable)
            db.execute('DELETE FROM mosaic_build WHERE build_id=?', (info['build_id'],))
            shutil.rmtree(out / 'builds' / info['build_id'], ignore_errors=True)
            reclaimed.append(f"builds/{info['build_id']}")
    return {'status': 'lost', 'block_id': block, 'seq': seq,
            'reason': 'duplicate_intent' if winner and winner['intent_id'] == intent_id else 'seq_taken',
            'build_id': info['build_id'] if info else None,
            'winner': winner and {'build_id': winner['build_id'], 'intent_id': winner['intent_id'],
                                  'state': winner['state'], 'publisher': winner['publisher']},
            'reclaimed': reclaimed}


def publish(db_path, file, out_dir, fail_at=None, hard=False, _sync=None):
    """mosaic-publish: protocol B -- intent, atomic install, verified confirm."""
    out = Path(out_dir)
    if _sync:
        _sync()
    with closing(connect(db_path)) as db:
        db.execute('BEGIN IMMEDIATE')
        info = block = seq = intent_id = None
        try:
            with db:
                doc, block, tiles, params = _load_request(db, file)
                seq, publisher = _publish_fields(doc)
                intent_id = doc['id']
                info, created = _ensure_build(db, out, block, tiles, params)
                row = db.execute('SELECT * FROM mosaic_publish WHERE block_id=? AND seq=?',
                                 (block, seq)).fetchone()
                if row:
                    if row['state'] == 'confirmed' and row['build_id'] == info['build_id'] \
                            and row['intent_id'] == intent_id:
                        return {'status': 'replayed', 'block_id': block, 'seq': seq,
                                'build_id': row['build_id'], 'matrix_sha': info['matrix_sha']}
                    if row['state'] == 'prepared' and row['intent_id'] == intent_id:
                        pass  # our own crashed attempt: retry the install below
                    elif row['state'] == 'abandoned':
                        db.execute("UPDATE mosaic_publish SET build_id=?, intent_id=?, publisher=?, "
                                   "state='prepared', prepared_at=CURRENT_TIMESTAMP, confirmed_at=NULL "
                                   "WHERE block_id=? AND seq=?",
                                   (info['build_id'], intent_id, publisher, block, seq))
                    else:
                        return _lost(db, out, block, seq, intent_id, info, row)
                else:
                    db.execute("INSERT INTO mosaic_publish(block_id,seq,build_id,intent_id,publisher,state) "
                               "VALUES(?,?,?,?,?,'prepared')",
                               (block, seq, info['build_id'], intent_id, publisher))
        except sqlite3.IntegrityError:
            doc = json.loads(Path(file).read_text(encoding='utf8'))
            return _lost(db, out, doc.get('block_id'), doc.get('seq'), doc.get('id'), info, None)
    _install_and_confirm(db_path, out, info, intent_id, block, seq, fail_at, hard)
    return {'status': 'published', 'block_id': block, 'seq': seq, 'build_id': info['build_id'],
            'matrix_sha': info['matrix_sha'], 'manifest_sha': info['manifest_sha']}


def _load_or_rebuild_candidate(db_path, out, info):
    cand_a = out / 'builds' / info['build_id'] / 'artifact.mosaic'
    cand_m = out / 'builds' / info['build_id'] / 'manifest.json'
    if cand_a.exists() and cand_m.exists():
        artifact, manifest = cand_a.read_bytes(), cand_m.read_bytes()
        if _sha(artifact) == info['matrix_sha'] and _sha(manifest) == info['manifest_sha']:
            return artifact, manifest
        cand_a.unlink()
        cand_m.unlink()  # corrupt cache: regenerate from the immutable source tiles
    with closing(connect(db_path)) as db:
        matrix = []
        for tid, tsha in zip(info['tile_ids'], info['tile_shas']):
            row = db.execute('SELECT s.raw, s.sha, t.hot_pixels, t.cloud_pixels FROM source s '
                             'JOIN tile_capture t ON t.source_id = s.id WHERE s.id = ?', (tid,)).fetchone()
            if not row or row['sha'] != tsha or _sha(row['raw']) != tsha:
                raise MosaicUnrecoverable(f'tile_source:{tid}')
            matrix.append([row['hot_pixels'], row['cloud_pixels']])
    artifact = _render_artifact(info['block_id'], info['params'], info['tile_ids'], matrix)
    if _sha(artifact) != info['matrix_sha']:
        raise MosaicUnrecoverable('matrix_digest')
    manifest = _render_manifest(info['build_id'], info['block_id'], info['params'],
                                info['params_sha'], info['tile_ids'], info['tile_shas'],
                                info['matrix_sha'])
    if _sha(manifest) != info['manifest_sha']:
        raise MosaicUnrecoverable('manifest_digest')
    _write_immutable(cand_a, artifact)
    _write_immutable(cand_m, manifest)
    return artifact, manifest


def _install_and_confirm(db_path, out, info, intent_id, block, seq, fail_at=None, hard=False):
    _fail('after_intent', fail_at, hard)
    artifact, manifest = _load_or_rebuild_candidate(db_path, out, info)
    stg_a = out / 'staging' / f'{intent_id}.artifact.tmp'
    stg_m = out / 'staging' / f'{intent_id}.manifest.tmp'
    _stage(stg_a, artifact)
    _stage(stg_m, manifest)
    _fail('after_stage', fail_at, hard)
    pub_a = out / 'published' / f"artifact-{info['matrix_sha']}.mosaic"
    pub_m = out / 'published' / f"manifest-{info['manifest_sha']}.json"
    _install(stg_a, pub_a, artifact)
    _install(stg_m, pub_m, manifest)
    _fail('after_install', fail_at, hard)
    with closing(connect(db_path)) as db:
        db.execute('BEGIN IMMEDIATE')
        with db:
            # the visible pointer is committed only if the installed bytes still match
            if not pub_a.exists() or _sha(pub_a.read_bytes()) != info['matrix_sha'] \
                    or not pub_m.exists() or _sha(pub_m.read_bytes()) != info['manifest_sha']:
                raise ValueError('mosaic_install_corrupt')
            cur = db.execute("UPDATE mosaic_publish SET state='confirmed', confirmed_at=CURRENT_TIMESTAMP "
                             "WHERE block_id=? AND seq=? AND intent_id=? AND state='prepared'",
                             (block, seq, intent_id))
            if cur.rowcount != 1:
                state = db.execute('SELECT state, build_id FROM mosaic_publish WHERE block_id=? AND seq=?',
                                   (block, seq)).fetchone()
                if not state or state['state'] != 'confirmed' or state['build_id'] != info['build_id']:
                    raise ValueError('mosaic_intent_state')
            db.execute("INSERT INTO mosaic_current(block_id,seq,build_id) VALUES(?,?,?) "
                       "ON CONFLICT(block_id) DO UPDATE SET seq=excluded.seq, build_id=excluded.build_id "
                       "WHERE excluded.seq > mosaic_current.seq", (block, seq, info['build_id']))
            _fail('before_confirm_commit', fail_at, hard)
    _fail('after_confirm_commit', fail_at, hard)


def _info_from_row(row):
    return {'build_id': row['build_id'], 'block_id': row['block_id'],
            'params': json.loads(row['params']), 'params_sha': row['params_sha'],
            'tile_ids': json.loads(row['tile_ids']), 'tile_shas': json.loads(row['tile_shas']),
            'matrix_sha': row['matrix_sha'], 'manifest_sha': row['manifest_sha'],
            'rows': row['rows'], 'cols': row['cols']}


def recover(db_path, out_dir):
    """mosaic-recover: retry or withdraw every prepared intent left by a crash."""
    out = Path(out_dir)
    with closing(connect(db_path)) as db:
        intents = [dict(r) for r in db.execute(
            "SELECT * FROM mosaic_publish WHERE state='prepared' ORDER BY block_id, seq")]
        builds = {b['build_id']: b for b in
                  (dict(r) for r in db.execute('SELECT * FROM mosaic_build'))}
    actions = []
    for it in intents:
        row = builds.get(it['build_id'])
        try:
            if not row:
                raise MosaicUnrecoverable('build_row_missing')
            _install_and_confirm(db_path, out, _info_from_row(row),
                                 it['intent_id'], it['block_id'], it['seq'])
            actions.append({'intent_id': it['intent_id'], 'seq': it['seq'],
                            'action': 'confirmed', 'build_id': it['build_id']})
        except MosaicUnrecoverable as exc:
            with closing(connect(db_path)) as db:
                db.execute('BEGIN IMMEDIATE')
                with db:
                    db.execute("UPDATE mosaic_publish SET state='abandoned' "
                               "WHERE block_id=? AND seq=? AND state='prepared'",
                               (it['block_id'], it['seq']))
            staging = out / 'staging'
            if staging.exists():
                for tmp in staging.glob(it['intent_id'] + '.*'):
                    tmp.unlink()
            actions.append({'intent_id': it['intent_id'], 'seq': it['seq'],
                            'action': 'abandoned', 'reason': str(exc)})
    return {'actions': actions, 'gc_candidates': _gc_candidates(db_path, out)}


def _gc_candidates(db_path, out):
    """Published files no build row references (protocol B pins everything else via intents)."""
    with closing(connect(db_path)) as db:
        referenced = set()
        for r in db.execute('SELECT matrix_sha, manifest_sha FROM mosaic_build'):
            referenced.add(f"artifact-{r['matrix_sha']}.mosaic")
            referenced.add(f"manifest-{r['manifest_sha']}.json")
    published = out / 'published'
    if not published.exists():
        return []
    return sorted(p.name for p in published.iterdir() if p.name not in referenced)


def verify(db_path, out_dir=None):
    """mosaic-verify: check the chain current -> publish -> build -> manifest -> artifact -> tiles."""
    problems = []
    with closing(connect(db_path)) as db:
        builds = {r['build_id']: dict(r) for r in db.execute('SELECT * FROM mosaic_build')}
        pubs = [dict(r) for r in db.execute('SELECT * FROM mosaic_publish')]
        currents = [dict(r) for r in db.execute('SELECT * FROM mosaic_current')]
        meta = db.execute("SELECT value FROM mosaic_meta WHERE key='out_dir'").fetchone()
        tiles = {r['source_id']: dict(r) for r in db.execute(
            'SELECT t.source_id, t.hot_pixels, t.cloud_pixels, s.sha, s.raw FROM tile_capture t '
            'JOIN source s ON s.id = t.source_id')}
    if not builds and not pubs and not currents:
        return {'status': 'absent', 'problems': []}
    out = Path(out_dir) if out_dir else (Path(meta['value']) if meta else None)
    if out is None:
        return {'status': 'tampered', 'problems': ['out_dir_unknown']}
    for bid, b in builds.items():
        tile_ids = json.loads(b['tile_ids'])
        tile_shas = json.loads(b['tile_shas'])
        params = json.loads(b['params'])
        if _sha(_canonical(params)) != b['params_sha']:
            problems.append(f'params_sha:{bid}')
        tiles_ok = True
        for tid, tsha in zip(tile_ids, tile_shas):
            t = tiles.get(tid)
            if not t or t['sha'] != tsha or _sha(t['raw']) != tsha:
                problems.append(f'tile_digest:{bid}:{tid}')
                tiles_ok = False
        if _build_digest(b['block_id'], b['params_sha'], tile_ids, tile_shas, b['matrix_sha']) != bid:
            problems.append(f'build_id:{bid}')
        if tiles_ok:
            matrix = [[tiles[t]['hot_pixels'], tiles[t]['cloud_pixels']] for t in tile_ids]
            if _sha(_render_artifact(b['block_id'], params, tile_ids, matrix)) != b['matrix_sha']:
                problems.append(f'matrix_recompute:{bid}')
    confirmed = {(p['block_id'], p['seq']): p for p in pubs if p['state'] == 'confirmed'}
    for c in currents:
        p = confirmed.get((c['block_id'], c['seq']))
        if not p or p['build_id'] != c['build_id'] or c['build_id'] not in builds:
            problems.append(f"current_pointer:{c['block_id']}")
    for p in confirmed.values():
        b = builds.get(p['build_id'])
        if not b:
            problems.append(f"publish_build:{p['intent_id']}")
            continue
        pub_a = out / 'published' / f"artifact-{b['matrix_sha']}.mosaic"
        pub_m = out / 'published' / f"manifest-{b['manifest_sha']}.json"
        if not pub_a.exists() or _sha(pub_a.read_bytes()) != b['matrix_sha']:
            problems.append(f"artifact:{p['block_id']}:{p['seq']}")
        if not pub_m.exists() or _sha(pub_m.read_bytes()) != b['manifest_sha']:
            problems.append(f"manifest:{p['block_id']}:{p['seq']}")
        else:
            m = json.loads(pub_m.read_bytes())
            if m.get('build_id') != p['build_id'] or m.get('matrix_sha') != b['matrix_sha'] \
                    or m.get('params_sha') != b['params_sha'] \
                    or [t.get('id') for t in m.get('tiles', [])] != json.loads(b['tile_ids']) \
                    or [t.get('sha') for t in m.get('tiles', [])] != json.loads(b['tile_shas']):
                problems.append(f"manifest_crossref:{p['block_id']}:{p['seq']}")
    return {'status': 'ok' if not problems else 'tampered',
            'problems': sorted(problems), 'builds': len(builds)}


def register_request(db_path, file):
    """Audit-replay hook: a mosaic request is preserved as evidence bytes only.

    Mosaic projections are deliberately NOT replayed from source bytes: the
    publish protocol spans the filesystem and the DB (see module docstring),
    so crash/recovery history cannot be reproduced by replay.  Integrity of
    mosaic outputs is checked by verify() instead and surfaced via audit().
    """
    with closing(connect(db_path)) as db, db:
        source(db, file, 'mosaic')
    return {'status': 'registered'}
