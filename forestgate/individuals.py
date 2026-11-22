"""Provable distinct-individual ranges for camera-trap observations.

A local track_hint is only meaningful inside its own camera, so the
provisional wildlife_count projection can neither merge one deer seen by
two cameras nor split two deer sharing one hint. Cross-camera identity is
a hypothesis that must survive, all at once: the shared power-on-session
clock offset (one offset per session, never one per image), maximum travel
speed between camera coverage discs, non-simultaneity, and confirmed
appearance marks (ear notches, antlers). Merges must be transitive and
globally consistent, never greedy pairwise: two tracks may each fit a
third while not fitting each other. When the evidence admits several
worlds, publish a versioned [lower, upper] range with one complete
feasible assignment per bound instead of a fake single value. Archived
RAW media and next-season synchronised co-sightings only ever append new
assessment versions; old quarterly reports and the provisional tally are
never rewritten.
"""
import json
from contextlib import closing
from datetime import datetime
from itertools import product
from math import factorial, hypot

from .store import connect, log, source

MAX_SPEED_KMH = 30.0
MEDIA_TIME_CLOSURE_S = 300
MAX_ATOMS = 10
MAX_INTERLEAVINGS = 4096


def _epoch(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def import_clock(db_path, file):
    """Register the shared clock-offset bounds of one camera power-on session."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'wildlife_clock')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        lo, hi = doc.get('offset_lo_s'), doc.get('offset_hi_s')
        camera = db.execute('SELECT id FROM wildlife_camera WHERE id=?', (doc.get('camera_id'),)).fetchone()
        if (camera is None or not doc.get('session')
                or not isinstance(lo, (int, float)) or isinstance(lo, bool)
                or not isinstance(hi, (int, float)) or isinstance(hi, bool) or lo > hi):
            raise ValueError('invalid_clock')
        if db.execute('SELECT 1 FROM wildlife_clock WHERE camera_id=? AND session=?',
                      (doc['camera_id'], doc['session'])).fetchone():
            raise ValueError('clock_session_conflict')
        db.execute('INSERT INTO wildlife_clock VALUES(?,?,?,?,?)',
                   (doc['id'], doc['camera_id'], doc['session'], lo, hi))
        log(db, 'wildlife', doc['camera_id'], 'clock_registered', f"{doc['session']}:[{lo},{hi}]s", doc['id'])
        return {'camera_id': doc['camera_id'], 'session': doc['session']}


def import_gap(db_path, file):
    """Record a camera outage window during which no frame could be captured."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'wildlife_gap')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        camera = db.execute('SELECT id FROM wildlife_camera WHERE id=?', (doc.get('camera_id'),)).fetchone()
        if camera is None or not doc.get('start_at') or not doc.get('end_at') or doc['start_at'] >= doc['end_at']:
            raise ValueError('invalid_gap')
        db.execute('INSERT INTO wildlife_gap VALUES(?,?,?,?)',
                   (doc['id'], doc['camera_id'], doc['start_at'], doc['end_at']))
        log(db, 'wildlife', doc['camera_id'], 'gap_registered', f"{doc['start_at']}..{doc['end_at']}", doc['id'])
        return {'camera_id': doc['camera_id'], 'start_at': doc['start_at'], 'end_at': doc['end_at']}


def import_media(db_path, file):
    """Attach an archived original image/video (e.g. a RAW side shot) to a frame.

    The media digest and archive reference are stored for traceability. A
    confirmed appearance mark only counts when the media source timestamp
    closes with the frame timestamp. Importing media never rewrites the
    provisional tally or any already published assessment version.
    """
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'wildlife_media')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        frame = db.execute('SELECT * FROM wildlife_frame WHERE source_id=?', (doc.get('frame_id'),)).fetchone()
        if frame is None:
            raise ValueError('unknown_frame')
        sha = doc.get('sha') or ''
        mark = doc.get('mark')
        if (doc.get('media_kind') not in ('image', 'video') or len(sha) != 64
                or any(c not in '0123456789abcdef' for c in sha)
                or not doc.get('shot_at')
                or (mark is not None and not (isinstance(mark, str) and mark))):
            raise ValueError('invalid_media')
        if abs(_epoch(doc['shot_at']) - _epoch(frame['at'])) > MEDIA_TIME_CLOSURE_S:
            raise ValueError('media_source_time_not_closed')
        db.execute('INSERT INTO wildlife_media VALUES(?,?,?,?,?,?,?)',
                   (doc['id'], doc['frame_id'], doc['media_kind'], sha, doc['shot_at'], mark, doc.get('archive_ref')))
        log(db, 'wildlife', doc['frame_id'], 'media_attached', mark or 'no_mark', doc['id'])
        return {'frame_id': doc['frame_id'], 'mark': mark}


def import_cosighting(db_path, file):
    """Record a synchronised overlapping-FOV co-sighting across cameras.

    Only a dual-camera setup with shared synchronisation and overlapping
    coverage can bind frames from different cameras to one individual. The
    constraint applies to the listed frames only; it never back-fills
    older quarters.
    """
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'wildlife_cosighting')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        frame_ids = doc.get('frame_ids') or []
        cameras = doc.get('cameras') or []
        frames = [db.execute('SELECT * FROM wildlife_frame WHERE source_id=?', (fid,)).fetchone()
                  for fid in frame_ids]
        if any(f is None for f in frames):
            raise ValueError('unknown_frame')
        if (len(frame_ids) < 2 or len(set(frame_ids)) != len(frame_ids)
                or not doc.get('synced') or not doc.get('overlap')
                or len(set(cameras)) < 2
                or not doc.get('valid_from') or not doc.get('valid_to') or doc['valid_from'] >= doc['valid_to']
                or any(f['camera_id'] not in cameras for f in frames)
                or len({f['camera_id'] for f in frames}) < 2
                or any(not doc['valid_from'] <= f['at'] <= doc['valid_to'] for f in frames)):
            raise ValueError('invalid_cosighting')
        for fid in frame_ids:
            db.execute('INSERT INTO wildlife_cosighting VALUES(?,?,?,?,?)',
                       (doc['id'], fid, json.dumps(sorted(cameras)), doc['valid_from'], doc['valid_to']))
        log(db, 'wildlife', doc['id'], 'cosighting_registered', ','.join(sorted(frame_ids)), doc['id'])
        return {'id': doc['id'], 'linked_frames': sorted(frame_ids), 'cameras': sorted(set(cameras))}


def _effective_km(a, b):
    """Distance a deer must cover between two camera coverage discs."""
    return max(0.0, hypot(a['x_km'] - b['x_km'], a['y_km'] - b['y_km'])
               - a['coverage_km'] - b['coverage_km'])


def _interleave_count(sequences):
    total = sum(len(s) for s in sequences)
    count = factorial(total)
    for s in sequences:
        count //= factorial(len(s))
    return count


def _interleave(sequences):
    sequences = [s for s in sequences if s]
    if not sequences:
        yield []
        return
    for i, seq in enumerate(sequences):
        rest = sequences[:i] + sequences[i + 1:] + ([seq[1:]] if len(seq) > 1 else [])
        for tail in _interleave(rest):
            yield [seq[0]] + tail


def _offsets_solvable(sequences, cameras, clocks, max_speed_kmh):
    """Test one shared offset choice per (camera, session), never per image.

    Every frame of a power-on session is shifted by the same offset. The
    system x_v in [lo_v, hi_v], x_w >= x_v + need(v, w) is solved exactly
    with longest paths; it is feasible iff the componentwise-minimal
    solution stays inside every session's bounds.
    """
    variables = sorted({(f[0], f[1]) for seq in sequences for f in seq})
    lo = {v: clocks.get(v, (0.0, 0.0))[0] for v in variables}
    hi = {v: clocks.get(v, (0.0, 0.0))[1] for v in variables}
    edges = []
    for seq in sequences:
        for a, b in zip(seq, seq[1:]):
            need_s = _effective_km(cameras[a[0]], cameras[b[0]]) / max_speed_kmh * 3600.0
            edges.append(((a[0], a[1]), (b[0], b[1]), need_s - (b[2] - a[2])))
    dist = dict(lo)
    for _ in range(len(variables)):
        changed = False
        for u, v, w in edges:
            if dist[u] + w > dist[v] + 1e-9:
                dist[v] = dist[u] + w
                changed = True
        if not changed:
            return all(dist[v] <= hi[v] + 1e-9 for v in variables)
    return False


def _partition_feasible(individuals, cameras, clocks, max_speed_kmh):
    """One world must serve every individual with the same session offsets."""
    per_individual = []
    budget = 1
    for individual in individuals:
        sequences = [[(f['camera_id'], f['session'], f['t']) for f in track['frames']]
                     for track in individual]
        budget *= _interleave_count(sequences)
        if budget > MAX_INTERLEAVINGS:
            raise ValueError('assessment_too_large')
        per_individual.append(list(_interleave(sequences)))
    return any(_offsets_solvable(list(combo), cameras, clocks, max_speed_kmh)
               for combo in product(*per_individual))


def _components(keys, links):
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for a, b in links:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def _partitions_of_size(n, size, conflicts):
    """Set partitions of range(n) into `size` blocks, canonically ordered,
    skipping blocks that join a must-not-merge pair of atoms."""
    blocks = []

    def place(i):
        if i == n:
            if len(blocks) == size:
                yield [sorted(b) for b in blocks]
            return
        for b in blocks:
            if not any((min(i, x), max(i, x)) in conflicts for x in b):
                b.append(i)
                yield from place(i + 1)
                b.pop()
        blocks.append([i])
        yield from place(i + 1)
        blocks.pop()

    yield from place(0)


def _assess(db, cameras, frames, max_speed_kmh):
    tracks = {}
    for f in frames:
        key = f"{f['camera_id']}:{f['track_hint']}"
        track = tracks.setdefault(key, {'key': key, 'camera_id': f['camera_id'], 'frames': [], 'marks': set()})
        track['frames'].append({'id': f['source_id'], 'camera_id': f['camera_id'],
                                'session': f['session'], 't': _epoch(f['at'])})
    for track in tracks.values():
        track['frames'].sort(key=lambda f: (f['t'], f['id']))
    frame_track = {f['id']: key for key, t in tracks.items() for f in t['frames']}
    for m in db.execute('SELECT * FROM wildlife_media'):
        if m['frame_id'] in frame_track and m['mark']:
            tracks[frame_track[m['frame_id']]]['marks'].add(m['mark'])
    cosightings = {}
    for row in db.execute('SELECT * FROM wildlife_cosighting'):
        if row['frame_id'] in frame_track:
            cosightings.setdefault(row['source_id'], set()).add(frame_track[row['frame_id']])
    links = []
    by_mark = {}
    for key, track in tracks.items():
        for mark in sorted(track['marks']):
            by_mark.setdefault(mark, []).append(key)
    for group in list(by_mark.values()) + [sorted(g) for g in cosightings.values() if len(g) >= 2]:
        for other in group[1:]:
            links.append((group[0], other))
    keys = sorted(tracks)
    atoms_keys = _components(keys, links)
    atom_of = {k: i for i, group in enumerate(atoms_keys) for k in group}
    conflicts = set()
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            ma, mb = tracks[a]['marks'], tracks[b]['marks']
            if ma and mb and ma.isdisjoint(mb):
                conflicts.add((min(atom_of[a], atom_of[b]), max(atom_of[a], atom_of[b])))
    if any(x == y for x, y in conflicts):
        raise ValueError('wildlife_evidence_conflict')
    atoms = [[tracks[k] for k in group] for group in atoms_keys]
    if len(atoms) > MAX_ATOMS:
        raise ValueError('assessment_too_large')
    clocks = {(r['camera_id'], r['session']): (r['offset_lo_s'], r['offset_hi_s'])
              for r in db.execute('SELECT * FROM wildlife_clock')}
    if not _partition_feasible(atoms, cameras, clocks, max_speed_kmh):
        raise ValueError('wildlife_evidence_conflict')
    lower = None
    for size in range(1, len(atoms)):
        for partition in _partitions_of_size(len(atoms), size, conflicts):
            individuals = [[track for ai in block for track in atoms[ai]] for block in partition]
            if _partition_feasible(individuals, cameras, clocks, max_speed_kmh):
                lower = partition
                break
        if lower is not None:
            break
    if lower is None:
        lower = [[i] for i in range(len(atoms))]

    def encode(individuals):
        return sorted((sorted(t['key'] for t in ind) for ind in individuals),
                      key=lambda b: (b[0], len(b)))

    lower_tracks = [[track for ai in block for track in atoms[ai]] for block in lower]
    return {'lower': len(lower), 'upper': len(atoms),
            'lower_assignment': encode(lower_tracks), 'upper_assignment': encode(atoms)}


def publish_assessment(db_path, file):
    """Compute and store a versioned [lower, upper] distinct-individual range.

    The provisional track_hint tally is a separate projection and is never
    touched. Publishing always appends a new version to the
    (block, species, season) series, so supplementary evidence can never
    silently overwrite an old quarterly report.
    """
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'wildlife_assessment')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        camera_ids = doc.get('camera_ids') or []
        cameras = {r['id']: dict(r) for r in db.execute('SELECT * FROM wildlife_camera')}
        if (not camera_ids or len(set(camera_ids)) != len(camera_ids)
                or any(c not in cameras for c in camera_ids)):
            raise ValueError('assessment_scope_unknown_camera')
        if any(cameras[c]['block_id'] != doc.get('block_id') for c in camera_ids):
            raise ValueError('assessment_scope_block_mismatch')
        max_speed = doc.get('max_speed_kmh', MAX_SPEED_KMH)
        if not isinstance(max_speed, (int, float)) or isinstance(max_speed, bool) or max_speed <= 0:
            raise ValueError('assessment_speed_invalid')
        if not doc.get('species') or not doc.get('season'):
            raise ValueError('assessment_fields')
        from_at, to_at = doc.get('from_at'), doc.get('to_at')
        if from_at and to_at and from_at > to_at:
            raise ValueError('assessment_window_invalid')
        frames = [dict(r) for r in db.execute('SELECT * FROM wildlife_frame WHERE species=?', (doc['species'],))
                  if r['camera_id'] in camera_ids
                  and (from_at is None or r['at'] >= from_at)
                  and (to_at is None or r['at'] <= to_at)]
        if not frames:
            raise ValueError('assessment_scope_empty')
        result = _assess(db, cameras, frames, max_speed)
        version = db.execute(
            'SELECT COUNT(*) FROM wildlife_assessment WHERE block_id=? AND species=? AND season=?',
            (doc['block_id'], doc['species'], doc['season'])).fetchone()[0] + 1
        frame_ids = sorted(f['source_id'] for f in frames)
        evidence = {
            'frames': frame_ids,
            'media': sorted(r['source_id'] for r in db.execute('SELECT * FROM wildlife_media')
                            if r['frame_id'] in frame_ids),
            'cosightings': sorted({r['source_id'] for r in db.execute('SELECT * FROM wildlife_cosighting')
                                   if r['frame_id'] in frame_ids}),
            'clocks': sorted(r['source_id'] for r in db.execute('SELECT * FROM wildlife_clock')
                             if r['camera_id'] in camera_ids),
            'gaps': sorted(r['source_id'] for r in db.execute('SELECT * FROM wildlife_gap')
                           if r['camera_id'] in camera_ids),
            'max_speed_kmh': max_speed,
            'scope': {'camera_ids': sorted(camera_ids), 'from_at': from_at, 'to_at': to_at},
        }
        db.execute('INSERT INTO wildlife_assessment VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (doc['id'], doc['block_id'], doc['species'], doc['season'], version,
                    result['lower'], result['upper'], json.dumps(result['lower_assignment']),
                    json.dumps(result['upper_assignment']), json.dumps(evidence, sort_keys=True)))
        log(db, 'wildlife', f"{doc['block_id']}:{doc['species']}:{doc['season']}",
            f"range[{result['lower']},{result['upper']}]", f'v{version}', doc['id'])
        return {'id': doc['id'], 'season': doc['season'], 'version': version,
                'lower': result['lower'], 'upper': result['upper'],
                'lower_assignment': result['lower_assignment'],
                'upper_assignment': result['upper_assignment']}
