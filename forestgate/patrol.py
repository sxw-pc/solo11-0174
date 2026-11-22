from .store import connect,log,source
from contextlib import closing

def import_shift(db_path,file):
    with closing(connect(db_path)) as db,db:
        doc,fresh=source(db,file,'shift')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if doc['start_at']>=doc['end_at'] or not doc['actor'] or not doc['team']:
            raise ValueError('shift_invalid')
        if db.execute('SELECT 1 FROM patrol_shift WHERE id=?',(doc['shift_id'],)).fetchone():raise ValueError('shift_id_conflict')
        db.execute('INSERT INTO patrol_shift VALUES(?,?,?,?,?,?,?)',(doc['shift_id'],doc['block_id'],doc['actor'],doc['team'],doc['start_at'],doc['end_at'],doc['id']))
        log(db,'shift',doc['shift_id'],'active','patrol_assignment',doc['id'])
        return {'shift_id':doc['shift_id'],'status':'active'}

def import_patrol(db_path,file):
    with closing(connect(db_path)) as db,db:
        doc,fresh=source(db,file,'patrol')
        if not fresh:return {'id':doc['id'],'replayed':True}
        block=db.execute('SELECT * FROM forest_block WHERE id=?',(doc['block_id'],)).fetchone()
        if not block:raise ValueError('unknown_block')
        if doc.get('area')!=block['area']:raise ValueError('fire_outside_block')
        action=doc['action']
        if action not in ('sighting','close','reopen') or not doc['actor'] or not doc['witness']:
            raise ValueError('patrol_fields')
        if doc['actor']==doc['witness']:raise ValueError('witness_not_independent')
        assignments={}
        for actor in (doc['actor'],doc['witness']):
            matches=db.execute('SELECT * FROM patrol_shift WHERE block_id=? AND actor=? AND start_at<=? AND end_at>?',
                               (doc['block_id'],actor,doc['at'],doc['at'])).fetchall()
            if len(matches)!=1:raise ValueError('missing_or_ambiguous_patrol_shift')
            assignments[actor]=matches[0]
        if assignments[doc['actor']]['team']==assignments[doc['witness']]['team']:
            raise ValueError('witness_same_team')
        event_id=doc['event_id'];old=db.execute('SELECT * FROM fire_state WHERE event_id=?',(event_id,)).fetchone()
        if action=='sighting' and old:raise ValueError('fire_already_reported')
        if action!='sighting' and (not old or old['block_id']!=doc['block_id']):raise ValueError('unknown_fire')
        if action=='close' and old['status']!='active':raise ValueError('fire_not_active')
        if action=='reopen' and old['status']!='closed':raise ValueError('fire_not_closed')
        prior=db.execute('SELECT * FROM fire_event WHERE event_id=? ORDER BY rowid DESC LIMIT 1',(event_id,)).fetchone()
        if prior and (doc['at']<=prior['at'] or (action=='close' and doc['actor'] in (prior['actor'],prior['witness']))):
            raise ValueError('fire_evidence_order_or_independence')
        db.execute('INSERT INTO fire_event VALUES(?,?,?,?,?,?,?)',(doc['id'],doc['block_id'],event_id,action,doc['actor'],doc['witness'],doc['at']))
        status={'sighting':'active','close':'closed','reopen':'active'}[action]
        version=(old['version']+1) if old else 1
        db.execute('INSERT INTO fire_state VALUES(?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET status=excluded.status,version=excluded.version,reason=excluded.reason',(event_id,doc['block_id'],status,version,action))
        log(db,'fire',event_id,status,action,doc['id'])
        from .access import recalc_access
        recalc_access(db,doc['block_id'],doc['id'])
        return {'id':doc['id'],'status':status,'version':version}
