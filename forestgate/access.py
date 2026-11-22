from .store import connect,log,source
from contextlib import closing

def recalc_access(db,block_id,source_id):
    block=db.execute('SELECT * FROM forest_block WHERE id=?',(block_id,)).fetchone()
    active=db.execute("SELECT segment FROM fire_state WHERE block_id=? AND status='active'",(block_id,)).fetchall()
    for request in db.execute('SELECT * FROM access_request WHERE block_id=?',(block_id,)).fetchall():
        if block['current_risk']=='unknown':status,reason='held','risk_untrusted'
        elif any(fire['segment']==request['route'] for fire in active):status,reason='suspended','active_fire'
        elif block['current_risk']=='high':status,reason='suspended','high_fire_risk'
        elif db.execute("SELECT 1 FROM decision_log WHERE system='access' AND object_id=? AND reason='high_fire_risk'",(request['request_id'],)).fetchone() and not db.execute('SELECT 1 FROM access_recheck WHERE request_id=?',(request['request_id'],)).fetchone():
            status,reason='held','recheck_after_risk_downgrade'
        elif request['work_type']=='hot' and not request['mitigation']:status,reason='held','mitigation_required'
        else:status,reason='granted','route_open'
        old=db.execute('SELECT * FROM access_state WHERE request_id=?',(request['request_id'],)).fetchone()
        if old and old['status']==status and old['reason']==reason:continue
        version=(old['version']+1) if old else 1
        db.execute('INSERT INTO access_state VALUES(?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,version=excluded.version',(request['request_id'],status,reason,version))
        log(db,'access',request['request_id'],status,reason,source_id)

def import_access(db_path,file):
    with closing(connect(db_path)) as db,db:
        doc,fresh=source(db,file,'access')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM forest_block WHERE id=?',(doc['block_id'],)).fetchone():raise ValueError('unknown_block')
        area=db.execute('SELECT area FROM forest_block WHERE id=?',(doc['block_id'],)).fetchone()['area']
        if not doc['route'].startswith(area+'-'):raise ValueError('route_outside_block')
        if doc['work_type'] not in ('hot','cold') or doc['start_at']>=doc['end_at'] or not doc['route']:
            raise ValueError('access_fields')
        if db.execute('SELECT 1 FROM access_request WHERE request_id=?',(doc['request_id'],)).fetchone():raise ValueError('request_id_conflict')
        db.execute('INSERT INTO access_request VALUES(?,?,?,?,?,?,?,?,?)',(doc['id'],doc['request_id'],doc['block_id'],doc['work_type'],doc['route'],doc['start_at'],doc['end_at'],int(bool(doc['mitigation'])),doc['actor']))
        recalc_access(db,doc['block_id'],doc['id'])
        return dict(db.execute('SELECT status,reason,version FROM access_state WHERE request_id=?',(doc['request_id'],)).fetchone())

def recheck_access(db_path,file):
    with closing(connect(db_path)) as db,db:
        doc,fresh=source(db,file,'recheck')
        if not fresh:return {'id':doc['id'],'replayed':True}
        request=db.execute('SELECT * FROM access_request WHERE request_id=?',(doc['request_id'],)).fetchone()
        if not request:raise ValueError('unknown_request')
        if doc['actor']==request['actor']:raise ValueError('self_recheck')
        if db.execute('SELECT 1 FROM access_recheck WHERE request_id=?',(doc['request_id'],)).fetchone():raise ValueError('recheck_already_recorded')
        state=db.execute('SELECT * FROM access_state WHERE request_id=?',(doc['request_id'],)).fetchone()
        if state['reason']!='recheck_after_risk_downgrade':raise ValueError('recheck_not_due')
        observed=db.execute('SELECT observed_at FROM risk_observation WHERE block_id=? ORDER BY seq DESC LIMIT 1',(request['block_id'],)).fetchone()
        if observed and doc['checked_at']<observed['observed_at']:raise ValueError('recheck_predates_risk')
        db.execute('INSERT INTO access_recheck VALUES(?,?,?,?)',(doc['id'],doc['request_id'],doc['actor'],doc['checked_at']))
        recalc_access(db,request['block_id'],doc['id'])
        return dict(db.execute('SELECT status,reason,version FROM access_state WHERE request_id=?',(doc['request_id'],)).fetchone())


def import_field_challenge(db_path,file):
    """Two independent route observations can dispute, but not replace, certified risk."""
    with closing(connect(db_path)) as db,db:
        doc,fresh=source(db,file,'field_challenge')
        if not fresh:return {'id':doc['id'],'replayed':True}
        request=db.execute('SELECT * FROM access_request WHERE request_id=?',(doc['request_id'],)).fetchone()
        risk=db.execute('SELECT * FROM risk_observation WHERE source_id=?',(doc['risk_source_id'],)).fetchone()
        if not request or not risk or risk['block_id']!=request['block_id']:
            raise ValueError('challenge_request_or_risk_unknown')
        latest=db.execute('SELECT source_id FROM risk_observation WHERE block_id=? ORDER BY seq DESC LIMIT 1',(request['block_id'],)).fetchone()
        if latest['source_id']!=risk['source_id'] or doc['observed_at']<risk['observed_at']:
            raise ValueError('challenge_stale_observation')
        if not isinstance(doc['dry'],int) or not 0<=doc['dry']<=100 or not doc['actor'] or not doc['plot_id'] or doc['actor']==request['actor']:
            raise ValueError('challenge_invalid_or_self_reported')
        prior=db.execute('SELECT * FROM field_challenge WHERE challenge_id=?',(doc['challenge_id'],)).fetchall()
        if len(prior)>=2 or any(x['request_id']!=doc['request_id'] or x['risk_source_id']!=doc['risk_source_id'] or x['actor']==doc['actor'] or x['plot_id']==doc['plot_id'] for x in prior):
            raise ValueError('challenge_conflict_or_not_independent')
        db.execute('INSERT INTO field_challenge VALUES(?,?,?,?,?,?,?,?)',(doc['id'],doc['challenge_id'],doc['request_id'],doc['risk_source_id'],doc['actor'],doc['plot_id'],doc['dry'],doc['observed_at']))
        if not prior:
            db.execute('INSERT INTO challenge_state VALUES(?,?,?,?,?)',(doc['challenge_id'],doc['request_id'],doc['risk_source_id'],'pending',1))
            return {'challenge_id':doc['challenge_id'],'status':'pending'}
        level='high' if min(prior[0]['dry'],doc['dry'])>=70 else ('medium' if min(prior[0]['dry'],doc['dry'])>=45 else 'low')
        status='confirmed_discrepancy' if level!=risk['level'] else 'not_discrepant'
        db.execute('UPDATE challenge_state SET status=?,version=version+1 WHERE challenge_id=?',(status,doc['challenge_id']))
        log(db,'challenge',doc['challenge_id'],status,level,doc['id'])
        if status=='confirmed_discrepancy':
            db.execute("UPDATE forest_block SET current_risk='unknown',risk_version=risk_version+1 WHERE id=?",(risk['block_id'],))
            log(db,'risk',risk['block_id'],'unknown','independent_field_challenge',doc['id'])
            recalc_access(db,risk['block_id'],doc['id'])
        return {'challenge_id':doc['challenge_id'],'status':status,'field_level':level}
