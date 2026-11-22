"""Mission reservations against declared stock and their physical revisions.

A reservation is a *commitment of the declaration ledger*: once written it is
never deleted or rewritten. Later tank evidence opens numbered revisions
(``fuel_mission_revision``) that say whether the commitment is physically
honour-able, short by how much, or still un-attested.

Rules enforced here:

* one dipstick reading without an attitude record can never mark a mission
  physically guaranteed — the tilted-tank range stays open;
* the guaranteed volume uses the conservative whole-litre *low* bound, so
  dead stock and measurement uncertainty are never optimised away;
* oil already committed to another mission is subtracted before a levelled
  flow-meter transfer can back a new mission — it is never counted twice;
* a levelling/flow-meter measurement taken now cannot backdate a commitment
  made under the old tilted legs.
"""
from contextlib import closing

from .store import connect, source, log
from .dip_volume import whole_litre_bounds


def _guaranteed_l(float_l):
    """Conservative whole-litre low bound, epsilon-stable for exact wedges."""
    return whole_litre_bounds(float_l, float_l)[0]


def _require_mission(db, mission_id):
    mission = db.execute('SELECT * FROM fuel_mission WHERE id=?', (mission_id,)).fetchone()
    if not mission:
        raise ValueError('fuel_mission_unknown')
    return mission


def _committed_l(db, depot_id, since=None, exclude_mission=None,
                 statuses=('reserved',)):
    """Litres demanded from this depot (no double counting).

    The declaration ledger may call a new mission ``short`` while a levelled
    flow-meter transfer later proves physical oil for it, so transfer-backed
    availability counts every mission demand, not only declaration-reserved
    ones.
    """
    placeholders = ','.join('?' for _ in statuses)
    query = ('SELECT COALESCE(SUM(need_l),0) AS total FROM fuel_mission '
             f'WHERE depot_id=? AND status IN ({placeholders})')
    params = [depot_id, *statuses]
    if since is not None:
        query += ' AND reserved_at>=?'
        params.append(since)
    if exclude_mission is not None:
        query += ' AND id<>?'
        params.append(exclude_mission)
    return db.execute(query, params).fetchone()['total']


def _next_revision(db, mission_id):
    return db.execute(
        'SELECT COALESCE(MAX(version),0)+1 AS v FROM fuel_mission_revision WHERE mission_id=?',
        (mission_id,)).fetchone()['v']


def _add_revision(db, mission, status, evidence_kind, evidence_source_id,
                  shortfall_l=None, at=None):
    if at is None:
        at = db.execute('SELECT CURRENT_TIMESTAMP AS t').fetchone()['t']
    version = _next_revision(db, mission['id'])
    db.execute(
        'INSERT INTO fuel_mission_revision(mission_id,version,status,shortfall_l,'
        'evidence_kind,evidence_source_id,at) VALUES(?,?,?,?,?,?,?)',
        (mission['id'], version, status, shortfall_l, evidence_kind,
         evidence_source_id, at))
    log(db, 'fuel_mission', mission['id'], status,
        f'revision_{version}', evidence_source_id)
    return {'mission_id': mission['id'], 'version': version, 'status': status,
            'shortfall_l': shortfall_l}


# ---------------------------------------------------------------------------
# Declaration-ledger reservation (baseline behaviour preserved)
# ---------------------------------------------------------------------------

def _apply_mission(db, doc, source_id):
    depot = db.execute('SELECT * FROM fuel_depot WHERE id=?', (doc['depot_id'],)).fetchone()
    if not depot or depot['block_id'] != doc['block_id']:
        raise ValueError('fuel_depot_unknown')
    need = doc['need_l']
    if not isinstance(need, int) or need <= 0:
        raise ValueError('mission_need_invalid')
    if doc.get('dip_id') is not None:
        dip = db.execute('SELECT * FROM tank_dip WHERE id=? AND depot_id=?',
                         (doc['dip_id'], depot['id'])).fetchone()
        if not dip:
            raise ValueError('mission_dip_unknown')
        if doc.get('reserved_at') is None:
            raise ValueError('mission_reserved_at_missing')
    status = 'reserved' if depot['stock_l'] >= need else 'short'
    if status == 'reserved':
        db.execute('UPDATE fuel_depot SET stock_l=stock_l-? WHERE id=?',
                   (need, depot['id']))
    db.execute(
        'INSERT INTO fuel_mission(id,block_id,depot_id,need_l,status,'
        'source_id,reserved_at,dip_id) VALUES(?,?,?,?,?,?,?,?)',
        (doc['mission_id'], doc['block_id'], depot['id'], need, status,
         source_id, doc.get('reserved_at'), doc.get('dip_id')))
    log(db, 'fuel_mission', doc['mission_id'], status,
        doc.get('reserved_at') or 'declared_stock_check', source_id)
    return {'mission_id': doc['mission_id'], 'status': status}


def reserve_mission(db_path, file):
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'fuel_mission')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _apply_mission(db, doc, doc['id'])


# ---------------------------------------------------------------------------
# Physical revisions triggered by tank evidence
# ---------------------------------------------------------------------------

def _revise_missions_for_dip(db, dip, tilt_source_id):
    """Open a gross-volume revision for every commitment tied to this dip."""
    corroboration = db.execute(
        'SELECT * FROM dip_corroboration WHERE dip_id=? AND attitude_source_id=? '
        'ORDER BY version DESC', (dip['id'], tilt_source_id)).fetchone()
    if not corroboration:
        return
    guaranteed_l = _guaranteed_l(corroboration['gross_low_l'])
    missions = db.execute('SELECT * FROM fuel_mission WHERE dip_id=?',
                          (dip['id'],)).fetchall()
    for mission in missions:
        already = db.execute(
            'SELECT 1 FROM fuel_mission_revision WHERE mission_id=? '
            'AND evidence_source_id=?', (mission['id'], tilt_source_id)).fetchone()
        if already:
            continue
        if mission['need_l'] <= guaranteed_l:
            _add_revision(db, mission, 'physical_verified', 'tilt', tilt_source_id,
                          at=dip['observed_at'])
        else:
            _add_revision(db, mission, 'physical_shortage', 'tilt', tilt_source_id,
                          shortfall_l=mission['need_l'] - guaranteed_l,
                          at=dip['observed_at'])


def _latest_corroboration(db, dip_id, kind, attitude_source_id=None):
    query = 'SELECT * FROM dip_corroboration WHERE dip_id=? AND kind=?'
    params = [dip_id, kind]
    if attitude_source_id is not None:
        query += ' AND attitude_source_id=?'
        params.append(attitude_source_id)
    return db.execute(query + ' ORDER BY version DESC', params).fetchone()


def corroborate_mission(db_path, file):
    """Attach an explicit physical verdict to an existing commitment."""
    with closing(connect(db_path)) as db, db:
        doc, fresh = source(db, file, 'mission_corroboration')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        kind = doc.get('evidence_kind')
        mission = _require_mission(db, doc['mission_id'])
        if kind in ('tilt', 'dip'):
            if not mission['dip_id']:
                raise ValueError('mission_has_no_dip')
            if kind == 'tilt':
                corroboration = _latest_corroboration(
                    db, mission['dip_id'], 'tilt', doc.get('attitude_source_id'))
                if not corroboration:
                    raise ValueError('attitude_not_corroborated')
                guaranteed_l = _guaranteed_l(corroboration['gross_low_l'])
                dip = db.execute('SELECT * FROM tank_dip WHERE id=?',
                                 (mission['dip_id'],)).fetchone()
                if mission['need_l'] <= guaranteed_l:
                    return _add_revision(db, mission, 'physical_verified', 'tilt',
                                         corroboration['attitude_source_id'],
                                         at=dip['observed_at'])
                return _add_revision(db, mission, 'physical_shortage', 'tilt',
                                     corroboration['attitude_source_id'],
                                     shortfall_l=mission['need_l'] - guaranteed_l,
                                     at=dip['observed_at'])
            corroboration = _latest_corroboration(db, mission['dip_id'], 'dip_only')
            if not corroboration:
                raise ValueError('attitude_not_corroborated')
            dip = db.execute('SELECT * FROM tank_dip WHERE id=?',
                             (mission['dip_id'],)).fetchone()
            # A lone dipstick can never guarantee the commitment.
            return _add_revision(db, mission, 'physical_unverified', 'dip', doc['id'],
                                 at=dip['observed_at'])
        if kind == 'transfer':
            transfer = db.execute(
                'SELECT * FROM level_transfer WHERE source_id=?',
                (doc.get('transfer_source_id'),)).fetchone()
            if not transfer:
                raise ValueError('transfer_unknown')
            if not mission['reserved_at'] or mission['reserved_at'] < transfer['observed_at']:
                raise ValueError('transfer_predates_commitment')
            guaranteed_l = transfer['extractable_l'] - transfer['metre_error_m']
            committed_others = _committed_l(
                db, transfer['depot_id'], since=transfer['observed_at'],
                exclude_mission=mission['id'],
                statuses=('reserved', 'short'))
            free_l = guaranteed_l - committed_others
            if mission['need_l'] <= free_l:
                return _add_revision(db, mission, 'physical_verified', 'transfer',
                                     transfer['source_id'], at=transfer['observed_at'])
            return _add_revision(db, mission, 'physical_shortage', 'transfer',
                                 transfer['source_id'],
                                 shortfall_l=mission['need_l'] - free_l,
                                 at=transfer['observed_at'])
        raise ValueError('evidence_kind_invalid')


# ---------------------------------------------------------------------------
# Current availability view (declaration vs. evidence, minus commitments)
# ---------------------------------------------------------------------------

def available_now(db_path, depot_id, at=None):
    """Declared free stock and the latest physical evidence, both net of
    commitments, so callers cannot promise the same litre twice."""
    with closing(connect(db_path)) as db:
        depot = db.execute('SELECT * FROM fuel_depot WHERE id=?', (depot_id,)).fetchone()
        if not depot:
            raise ValueError('fuel_depot_unknown')
        dip = db.execute(
            'SELECT * FROM tank_dip WHERE depot_id=?' +
            (' AND observed_at<=?' if at is not None else '') +
            ' ORDER BY observed_at DESC,id DESC',
            [depot_id] + ([at] if at is not None else [])).fetchone()
        view = {
            'depot_id': depot_id,
            'declared_stock_l': depot['stock_l'],
            'committed_l': _committed_l(db, depot_id),
            'latest_dip': None,
            'transfer': None,
        }
        if dip:
            corroboration = db.execute(
                'SELECT * FROM dip_corroboration WHERE dip_id=? ORDER BY version DESC',
                (dip['id'],)).fetchone()
            committed_at_dip = db.execute(
                "SELECT COALESCE(SUM(need_l),0) AS total FROM fuel_mission "
                "WHERE depot_id=? AND status='reserved' AND reserved_at IS NOT NULL "
                'AND reserved_at<=?', (depot_id, dip['observed_at'])).fetchone()['total']
            view['latest_dip'] = {
                'dip_id': dip['id'],
                'epoch': dip['epoch'],
                'observed_at': dip['observed_at'],
                'kind': corroboration['kind'],
                'gross_l': [corroboration['gross_low_l'], corroboration['gross_high_l']],
                'extractable_l': [corroboration['extractable_low_l'],
                                  corroboration['extractable_high_l']],
                'committed_at_dip_l': committed_at_dip,
                'free_extractable_low_l':
                    corroboration['extractable_low_l'] - committed_at_dip,
            }
        transfer = db.execute(
            'SELECT * FROM level_transfer WHERE depot_id=?' +
            (' AND observed_at<=?' if at is not None else '') +
            ' ORDER BY observed_at DESC,source_id DESC',
            [depot_id] + ([at] if at is not None else [])).fetchone()
        if transfer:
            committed_after = _committed_l(
                db, depot_id, since=transfer['observed_at'],
                statuses=('reserved', 'short'))
            low = transfer['extractable_l'] - transfer['metre_error_m']
            high = transfer['extractable_l'] + transfer['metre_error_m']
            view['transfer'] = {
                'source_id': transfer['source_id'],
                'observed_at': transfer['observed_at'],
                'extractable_low_l': low,
                'extractable_high_l': high,
                'committed_after_l': committed_after,
                'free_low_l': low - committed_after,
                'free_high_l': high - committed_after,
            }
        return view
