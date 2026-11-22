"""Feasible strike areas and patrol coverage publication.

For every grouped event the feasible set of (strike point, emission time, clock
offsets) is constructed under the versioned site constants. A receiver's clock
offset is one shared parameter for its whole boot session, so events that share
a clock-evidence baseline are solved jointly: no per-pulse clock shopping. The
jointly feasible samples are mapped onto the registered compartments; a sample
landing on a compartment boundary puts every touching compartment on the
pending list. Publishing a release stores an immutable, versioned snapshot of
pending compartments, shift coverage and uncovered todos, so recalibration
(GNSS recovery) or a new receiver produces a new release instead of silently
rewriting the patrol ranges dispatched earlier.
"""
import json
import math
from contextlib import closing
from datetime import datetime, timedelta
from .store import connect, log, source
from .patrol import shift_coverage

GRID_LO_KM, GRID_HI_KM = -25.0, 35.0
COARSE_STEP_KM, FINE_STEP_KM = 0.5, 0.05
COARSE_TOL_US, FINE_TOL_US = 3.0, 0.3
TRUSTED_MAX_UNCERTAINTY_US = 1.0
BOUNDARY_KM = 0.15


def feasible_samples(members, receivers, clocks, speed):
    """Grid scan of every (x, y, t0, offsets) consistent with the arrivals.

    Trusted clocks bound the emission-time interval directly; for each uncertain
    clock the required offset is solved and checked against its registered
    uncertainty. ``members`` are rows with receiver_id/boot_id/arrival_us.
    """
    obs = []
    for m in members:
        clock = clocks[(m['receiver_id'], m['boot_id'])]
        rx = receivers[m['receiver_id']]
        obs.append({'rid': m['receiver_id'], 'x': rx['x_km'], 'y': rx['y_km'], 'arr': m['arrival_us'],
                    'off': clock['offset_us'], 'unc': clock['uncertainty_us']})
    trusted = [o for o in obs if o['unc'] <= TRUSTED_MAX_UNCERTAINTY_US]
    uncertain = [o for o in obs if o['unc'] > TRUSTED_MAX_UNCERTAINTY_US]
    if not trusted:
        return []

    def check(x, y, tol):
        lo, hi = -math.inf, math.inf
        for o in trusted:
            t = o['arr'] - math.hypot(x - o['x'], y - o['y']) / speed - o['off']
            lo, hi = max(lo, t - o['unc']), min(hi, t + o['unc'])
        if lo > hi + tol:
            return None
        t0, spread = (lo + hi) / 2, (hi - lo) / 2
        deltas = {}
        for o in uncertain:
            req = o['arr'] - t0 - math.hypot(x - o['x'], y - o['y']) / speed - o['off']
            if abs(req) > o['unc'] + spread + tol:
                return None
            deltas[o['rid']] = req
        return (x, y, t0, deltas)

    coarse_hits = []
    steps = int(round((GRID_HI_KM - GRID_LO_KM) / COARSE_STEP_KM))
    for i in range(steps + 1):
        for j in range(steps + 1):
            hit = check(GRID_LO_KM + i * COARSE_STEP_KM, GRID_LO_KM + j * COARSE_STEP_KM, COARSE_TOL_US)
            if hit:
                coarse_hits.append(hit)
    fine = {}
    span = COARSE_STEP_KM
    sub = int(round(2 * span / FINE_STEP_KM))
    for cx, cy, _, _ in coarse_hits:
        for i in range(sub + 1):
            for j in range(sub + 1):
                x, y = cx - span + i * FINE_STEP_KM, cy - span + j * FINE_STEP_KM
                key = (round(x, 6), round(y, 6))
                if key not in fine:
                    hit = check(x, y, FINE_TOL_US)
                    if hit:
                        fine[key] = hit
    return sorted(fine.values(), key=lambda s: (s[0], s[1]))


def _joint_filter(per_event):
    """Intersect the per-event feasible offset ranges of one shared boot baseline."""
    rids = sorted({rid for samples in per_event.values() for s in samples for rid in s[3]})
    joint = {}
    for rid in rids:
        lows = [min(s[3][rid] for s in samples if rid in s[3])
                for samples in per_event.values() if any(rid in s[3] for s in samples)]
        highs = [max(s[3][rid] for s in samples if rid in s[3])
                 for samples in per_event.values() if any(rid in s[3] for s in samples)]
        if lows:
            joint[rid] = (max(lows), min(highs))
    filtered = {}
    for eid, samples in per_event.items():
        filtered[eid] = [s for s in samples
                         if all(joint[rid][0] - 1e-9 <= s[3][rid] <= joint[rid][1] + 1e-9 for rid in s[3])]
    return joint, filtered


def _inside(x, y, poly):
    inside = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y):
            if x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def _near_boundary(x, y, poly):
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if not length2 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length2))
        if math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)) < BOUNDARY_KM:
            return True
    return False


def _compartments_of(samples, compartments):
    found = set()
    for x, y, _, _ in samples:
        for comp in compartments:
            if _inside(x, y, comp['polygon']) or _near_boundary(x, y, comp['polygon']):
                found.add(comp['id'])
    return found


def _delta_ranges(samples):
    ranges = {}
    for _, _, _, deltas in samples:
        for rid, value in deltas.items():
            lo, hi = ranges.get(rid, (value, value))
            ranges[rid] = (min(lo, value), max(hi, value))
    return ranges


def _event_time(members, clocks, t0_us):
    trusted = [m for m in members
               if clocks[(m['receiver_id'], m['boot_id'])]['uncertainty_us'] <= TRUSTED_MAX_UNCERTAINTY_US]
    ref = trusted[0] if trusted else members[0]
    started = clocks[(ref['receiver_id'], ref['boot_id'])]['boot_started_at']
    base = datetime.fromisoformat(started.replace('Z', '+00:00'))
    return (base + timedelta(microseconds=round(t0_us))).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'


def publish_release(db_path, file):
    """Compute the joint feasible sets and publish an immutable coverage release."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'lightning_release')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        site = db.execute('SELECT * FROM lightning_site ORDER BY rowid LIMIT 1').fetchone()
        if site is None:
            raise ValueError('site_required')
        speed = site['speed_km_per_us']
        receivers = {r['id']: r for r in db.execute('SELECT * FROM lightning_receiver').fetchall()}
        clocks = {}
        for row in db.execute('SELECT * FROM lightning_clock ORDER BY version').fetchall():
            clocks[(row['receiver_id'], row['boot_id'])] = row
        compartments = [{'id': r['id'], 'block_id': r['block_id'], 'polygon': json.loads(r['polygon'])}
                        for r in db.execute('SELECT * FROM lightning_compartment ORDER BY id').fetchall()]
        events = db.execute('SELECT * FROM lightning_event ORDER BY seq').fetchall()
        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM lightning_release').fetchone()[0]

        computed = {}
        for event in events:
            members = [dict(m) for m in db.execute(
                'SELECT * FROM lightning_event_pulse WHERE event_id=? ORDER BY pulse_source_id', (event['id'],)).fetchall()]
            if len(members) < 3:
                computed[event['id']] = {'status': 'fragment', 'members': members, 'samples': [], 'baseline': None}
                continue
            baseline = tuple(sorted((m['receiver_id'], m['boot_id'], clocks[(m['receiver_id'], m['boot_id'])]['version'])
                                    for m in members))
            samples = feasible_samples(members, receivers, clocks, speed)
            computed[event['id']] = {'status': 'located', 'members': members, 'samples': samples, 'baseline': baseline}

        groups = {}
        for eid, c in computed.items():
            if c['baseline']:
                groups.setdefault(c['baseline'], []).append(eid)
        joint_ranges, joint_samples = {}, {}
        for baseline, eids in groups.items():
            joint, filtered = _joint_filter({eid: computed[eid]['samples'] for eid in eids})
            for eid in eids:
                joint_ranges[eid], joint_samples[eid] = joint, filtered[eid]

        release_id = doc['id']
        unverified, todos, pending_union = 0, 0, set()
        event_rows, compartment_rows, todo_rows = [], [], []
        for event in events:
            eid = event['id']
            c = computed[eid]
            if c['status'] == 'fragment':
                event_rows.append((release_id, eid, 'fragment', None, '[]', '[]', '{}', '{}'))
                continue
            own = _compartments_of(c['samples'], compartments)
            samples = joint_samples[eid]
            pending = _compartments_of(samples, compartments)
            status = 'resolved' if len(pending) == 1 else ('ambiguous' if pending else 'inconsistent')
            if status != 'resolved':
                unverified += 1
            event_time = _event_time(c['members'], clocks, sum(s[2] for s in samples) / len(samples)) if samples else None
            event_rows.append((release_id, eid, status, event_time, json.dumps(sorted(own)), json.dumps(sorted(pending)),
                               json.dumps({k: [v[0], v[1]] for k, v in joint_ranges[eid].items()}),
                               json.dumps({k: [v[0], v[1]] for k, v in _delta_ranges(c['samples']).items()})))
            block_of = {comp['id']: comp['block_id'] for comp in compartments}
            for comp_id in sorted(pending):
                block_id = block_of[comp_id]
                shifts = shift_coverage(db, block_id, event_time)
                if shifts:
                    compartment_rows.append((release_id, eid, comp_id, block_id, 'covered', shifts[0]['id'], shifts[0]['team']))
                else:
                    compartment_rows.append((release_id, eid, comp_id, block_id, 'uncovered', None, None))
                    todo_rows.append((release_id, eid, comp_id, block_id, 'no_covering_shift'))
                    todos += 1
            pending_union |= set(pending)
        db.execute('INSERT INTO lightning_release VALUES(?,?,?,?,?,?,?,?,?)',
                   (release_id, version, doc['id'], site['coord_version'], site['time_unit'], speed,
                    len(events), unverified, json.dumps(sorted(pending_union))))
        db.executemany('INSERT INTO lightning_release_event VALUES(?,?,?,?,?,?,?,?)', event_rows)
        db.executemany('INSERT INTO lightning_release_compartment VALUES(?,?,?,?,?,?,?)', compartment_rows)
        db.executemany('INSERT INTO lightning_release_todo VALUES(?,?,?,?,?)', todo_rows)
        log(db, 'lightning_release', release_id, 'published', f'version_{version}', doc['id'])
        return {'release_id': release_id, 'version': version, 'unverified_events': unverified,
                'pending_compartments': sorted(pending_union), 'todos': todos}
