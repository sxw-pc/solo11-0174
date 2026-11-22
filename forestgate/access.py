from .store import connect,log,source
from contextlib import closing

def recalc_access(db,block_id,source_id):
    block=db.execute('SELECT * FROM forest_block WHERE id=?',(block_id,)).fetchone()
    active=db.execute("SELECT COUNT(*) FROM fire_state WHERE block_id=? AND status='active'",(block_id,)).fetchone()[0]
    for request in db.execute('SELECT * FROM access_request WHERE block_id=?',(block_id,)).fetchall():
        if block['current_risk']=='unknown':status,reason='held','risk_untrusted'
        elif active:status,reason='suspended','active_fire'
        elif block['current_risk']=='high':status,reason='suspended','high_fire_risk'
        elif (old:=db.execute('SELECT * FROM access_state WHERE request_id=?',(request['request_id'],)).fetchone()) and old['reason']=='high_fire_risk' and not db.execute('SELECT 1 FROM access_recheck WHERE request_id=?',(request['request_id'],)).fetchone():
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
        if area not in doc['route']:raise ValueError('route_outside_block')
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
        db.execute('INSERT INTO access_recheck VALUES(?,?,?,?)',(doc['id'],doc['request_id'],doc['actor'],doc['checked_at']))
        recalc_access(db,request['block_id'],doc['id'])
        return dict(db.execute('SELECT status,reason,version FROM access_state WHERE request_id=?',(doc['request_id'],)).fetchone())
