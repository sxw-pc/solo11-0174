import hashlib
import json
from datetime import datetime
from pathlib import Path
from forestgate.lightning import import_site, import_receiver, import_clock, import_compartment, import_pulse
from forestgate.patrol import import_shift
from forestgate.strike_area import publish_release
from forestgate.store import report, connect
from forestgate.audit import audit

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'lightning-ambiguous.json'
# C offset that makes Q=(2,-3) mimic P=(2,3) on identical raw times: (sqrt(53)-sqrt(173))/0.3 us.
Q_WORLD_CLOCK_US = -19.576122


def scenario():
    return json.loads(FIXTURE.read_text(encoding='utf8'))


def write(tmp_path, doc):
    file = tmp_path / f"{doc['id']}.json"
    file.write_text(json.dumps(doc), encoding='utf8')
    return file


def import_doc(db, tmp_path, doc):
    fn = {'lightning_site': import_site, 'lightning_receiver': import_receiver,
          'lightning_clock': import_clock, 'lightning_compartment': import_compartment,
          'lightning_pulse': import_pulse, 'shift': import_shift}[doc['kind']]
    return fn(db, write(tmp_path, doc))


def import_base(db, tmp_path, data):
    for doc in data['base']:
        import_doc(db, tmp_path, doc)


def import_pulses(db, tmp_path, data, ids):
    return {pid: import_doc(db, tmp_path, data['pulses'][pid]) for pid in ids}


def publish(db, tmp_path, release_id):
    return publish_release(db, write(tmp_path, {'kind': 'lightning_release', 'id': release_id}))


def release_events(db, release_id):
    return {r['event_id']: r for r in report(db)['lightning_release_events'] if r['release_id'] == release_id}


def test_pq_identical_raw_times_keep_north_and_south_pending(tmp_path):
    db = tmp_path / 'forest.sqlite'
    data = scenario()
    import_base(db, tmp_path, data)
    import_pulses(db, tmp_path, data, data['events']['event1'])
    out = publish(db, tmp_path, 'rel-1')
    assert out['unverified_events'] == 1
    assert out['pending_compartments'] == ['CMP-N', 'CMP-S']
    row = release_events(db, 'rel-1')['evt-1']
    assert row['status'] == 'ambiguous'
    assert json.loads(row['own_compartments']) == ['CMP-N', 'CMP-S']
    assert json.loads(row['pending_compartments']) == ['CMP-N', 'CMP-S']
    event_at = datetime.fromisoformat(row['event_time'].replace('Z', '+00:00'))
    expected = datetime.fromisoformat('2026-11-01T10:00:00+00:00')
    assert abs((event_at - expected).total_seconds()) < 5  # t0 is recovered, not asserted exactly
    joint_c = json.loads(row['joint_delta_us'])['C']
    assert joint_c[0] < Q_WORLD_CLOCK_US < joint_c[1]  # the Q world still fits the same raw times
    rep = report(db)
    cover = {(r['event_id'], r['compartment_id']): r for r in rep['lightning_release_compartments']}
    assert cover[('evt-1', 'CMP-N')]['coverage'] == 'covered'
    assert cover[('evt-1', 'CMP-N')]['team'] == 'team-north'
    assert cover[('evt-1', 'CMP-S')]['coverage'] == 'uncovered'  # expired shift is not a dispatched team
    todos = [(t['event_id'], t['compartment_id'], t['reason']) for t in rep['lightning_release_todos']]
    assert todos == [('evt-1', 'CMP-S', 'no_covering_shift')]
    assert audit(db)['status'] == 'matched'


def test_shared_boot_offset_forbids_picking_per_event_clocks(tmp_path):
    db = tmp_path / 'forest.sqlite'
    data = scenario()
    import_base(db, tmp_path, data)
    import_pulses(db, tmp_path, data, data['events']['event1'])
    import_pulses(db, tmp_path, data, data['events']['event2'])
    out = publish(db, tmp_path, 'rel-1')
    assert out['unverified_events'] == 1
    rows = release_events(db, 'rel-1')
    # Event 1 alone would allow both worlds; the boot-shared offset kills its southern one.
    assert json.loads(rows['evt-1']['own_compartments']) == ['CMP-N', 'CMP-S']
    assert json.loads(rows['evt-1']['pending_compartments']) == ['CMP-N']
    assert json.loads(rows['evt-2']['pending_compartments']) == ['CMP-N', 'CMP-S']
    own_c = json.loads(rows['evt-1']['own_delta_us'])['C']
    joint_c = json.loads(rows['evt-1']['joint_delta_us'])['C']
    assert own_c[0] < Q_WORLD_CLOCK_US < own_c[1]  # per-event clock shopping could reach the Q world
    assert joint_c[0] > -2.0                       # the shared boot offset cannot
    assert joint_c[1] > 19.0
    assert audit(db)['status'] == 'matched'


def test_grouping_uses_waveform_and_physics_not_page_order(tmp_path):
    db = tmp_path / 'forest.sqlite'
    data = scenario()
    import_base(db, tmp_path, data)
    results = import_pulses(db, tmp_path, data, data['scrambled_import_order'])
    rep = report(db)
    members = {}
    for ep in rep['lightning_event_pulses']:
        members.setdefault(ep['event_id'], set()).add(ep['pulse_source_id'])
    grouped = sorted(tuple(sorted(m)) for m in members.values() if len(m) == 3)
    assert grouped == [('p1A', 'p1B', 'p1C'), ('p2A', 'p2B', 'p2C')]
    fragments = sorted(next(iter(m)) for m in members.values() if len(m) == 1)
    assert fragments == ['pD1', 'pD2']  # mismatched waveform / impossible timing stay unmerged
    # The late page (page_seq 1, imported last) still lands in its evidence event.
    assert results['p1C']['event_id'] == results['p1A']['event_id']
    assert results['p1C']['event_id'] != results['p2C']['event_id']
    out = publish(db, tmp_path, 'rel-1')
    rows = release_events(db, 'rel-1')
    by_id = {eid: row for eid, row in rows.items()}
    frag_status = sorted(row['status'] for row in by_id.values() if row['status'] == 'fragment')
    assert frag_status == ['fragment', 'fragment']
    assert audit(db)['status'] == 'matched'


def test_gnss_recovery_narrows_historical_batch(tmp_path):
    db = tmp_path / 'forest.sqlite'
    data = scenario()
    import_base(db, tmp_path, data)
    import_pulses(db, tmp_path, data, data['events']['event1'])
    v1 = publish(db, tmp_path, 'rel-1')
    assert v1['pending_compartments'] == ['CMP-N', 'CMP-S']
    clock = import_doc(db, tmp_path, data['gnss_recovery'][0])
    assert clock['version'] == 2
    v2 = publish(db, tmp_path, 'rel-2')
    assert v2['unverified_events'] == 0
    assert v2['pending_compartments'] == ['CMP-N']
    rows = release_events(db, 'rel-2')
    assert rows['evt-1']['status'] == 'resolved'
    assert json.loads(rows['evt-1']['pending_compartments']) == ['CMP-N']
    # The old release keeps the patrol range dispatched at the time; history is not rewritten.
    assert json.loads(release_events(db, 'rel-1')['evt-1']['pending_compartments']) == ['CMP-N', 'CMP-S']
    rep = report(db)
    assert [r['version'] for r in rep['lightning_releases']] == [1, 2]
    todos = {(t['release_id'], t['compartment_id']) for t in rep['lightning_release_todos']}
    assert todos == {('rel-1', 'CMP-S')}  # the south todo is cancelled by evidence, not deleted
    clocks = [(c['clock_source'], c['uncertainty_us'], c['version'])
              for c in rep['lightning_clocks'] if c['receiver_id'] == 'C']
    assert clocks == [('free', 20.0, 1), ('gnss_recovered', 1.0, 2)]
    with connect(db) as con:
        for raw_row in con.execute('SELECT sha, raw FROM source'):
            assert hashlib.sha256(raw_row['raw']).hexdigest() == raw_row['sha']
    assert audit(db)['status'] == 'matched'


def test_new_receiver_d_resolves_only_future_events(tmp_path):
    db = tmp_path / 'forest.sqlite'
    data = scenario()
    import_base(db, tmp_path, data)
    import_pulses(db, tmp_path, data, data['events']['event1'])
    v1 = publish(db, tmp_path, 'rel-1')
    assert v1['unverified_events'] == 1
    for doc in data['receiver_d']:
        import_doc(db, tmp_path, doc)
    import_pulses(db, tmp_path, data, data['events']['event3'])
    v2 = publish(db, tmp_path, 'rel-2')
    assert v2['unverified_events'] == 1
    assert v2['pending_compartments'] == ['CMP-N', 'CMP-S']
    rows = release_events(db, 'rel-2')
    assert json.loads(rows['evt-1']['pending_compartments']) == ['CMP-N', 'CMP-S']  # old pulses keep both
    assert json.loads(rows['evt-2']['pending_compartments']) == ['CMP-N']           # D constrains only new ones
    rep = report(db)
    members = {}
    for ep in rep['lightning_event_pulses']:
        members.setdefault(ep['event_id'], set()).add(ep['pulse_source_id'])
    assert members['evt-1'] == {'p1A', 'p1B', 'p1C'}  # D's reading is never backfilled into old events
    assert members['evt-2'] == {'p3A', 'p3B', 'p3C', 'p3D'}
    todos = {(t['release_id'], t['event_id'], t['compartment_id']) for t in rep['lightning_release_todos']}
    assert ('rel-2', 'evt-1', 'CMP-S') in todos
    assert audit(db)['status'] == 'matched'
