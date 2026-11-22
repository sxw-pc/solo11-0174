from .store import connect,log,source
from contextlib import closing

def import_station(db_path,file):
    with closing(connect(db_path)) as db,db:
        doc,fresh=source(db,file,'station')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not doc['block_id'] or not doc['station'] or doc['valid_from']>=doc['valid_to']:
            raise ValueError('station_contract_invalid')
        old=db.execute('SELECT * FROM station_cert WHERE id=?',(doc['cert_id'],)).fetchone()
        if old:raise ValueError('station_cert_conflict')
        db.execute('INSERT INTO station_cert VALUES(?,?,?,?,?,?,?)',(doc['cert_id'],doc['block_id'],doc['station'],doc['valid_from'],doc['valid_to'],'active',doc['id']))
        log(db,'station',doc['cert_id'],'active','calibration_window_registered',doc['id'])
        return {'cert_id':doc['cert_id'],'status':'active'}

def revoke_station(db_path,file):
    with closing(connect(db_path)) as db,db:
        doc,fresh=source(db,file,'station_revoke')
        if not fresh:return {'id':doc['id'],'replayed':True}
        cert=db.execute('SELECT * FROM station_cert WHERE id=?',(doc['cert_id'],)).fetchone()
        if not cert or cert['status']!='active':raise ValueError('station_not_active')
        db.execute("UPDATE station_cert SET status='revoked' WHERE id=?",(doc['cert_id'],))
        block=db.execute('SELECT * FROM forest_block WHERE id=?',(cert['block_id'],)).fetchone()
        if block and block['station']==cert['station']:
            db.execute("UPDATE forest_block SET current_risk='unknown',risk_version=risk_version+1 WHERE id=?",(cert['block_id'],))
            log(db,'risk',cert['block_id'],'unknown','station_revoked',doc['id'])
            from .access import recalc_access
            recalc_access(db,cert['block_id'],doc['id'])
        return {'cert_id':doc['cert_id'],'status':'revoked'}

def import_risk(db_path,file):
    with closing(connect(db_path)) as db,db:
        doc,fresh=source(db,file,'risk')
        if not fresh:return {'id':doc['id'],'replayed':True}
        bid=doc['block_id'];seq=doc['seq']
        if not isinstance(seq,int) or seq<1 or doc['level'] not in ('low','medium','high'):
            raise ValueError('risk_fields')
        old=db.execute('SELECT * FROM forest_block WHERE id=?',(bid,)).fetchone()
        if old and (old['area']!=doc['area'] or old['station']!=doc['station']):raise ValueError('block_identity_conflict')
        if not old:db.execute('INSERT INTO forest_block(id,area,station) VALUES(?,?,?)',(bid,doc['area'],doc['station']))
        same=db.execute('SELECT * FROM risk_observation WHERE block_id=? AND seq=?',(bid,seq)).fetchone()
        if same:raise ValueError('risk_revision_conflict')
        cert=db.execute('SELECT * FROM station_cert WHERE id=?',(doc.get('cert_id'),)).fetchone()
        valid=int(bool(doc['station_valid']) and cert is not None and cert['status']=='active'
                  and cert['block_id']==bid and cert['station']==doc['station']
                  and cert['valid_from']<=doc['observed_at']<cert['valid_to'])
        a,b=doc.get('dry_a'),doc.get('dry_b')
        status='trusted' if valid and isinstance(a,int) and isinstance(b,int) and a>=0 and b>=0 and doc.get('sample_a')!=doc.get('sample_b') else 'untrusted'
        if status=='trusted':
            computed='high' if min(a,b)>=70 else ('medium' if min(a,b)>=45 else 'low')
            if computed!=doc['level']:status='level_mismatch'
        db.execute('INSERT INTO risk_observation VALUES(?,?,?,?,?,?,?)',(doc['id'],bid,seq,valid,a,b,doc['level']))
        current=db.execute('SELECT MAX(seq) FROM risk_observation WHERE block_id=?',(bid,)).fetchone()[0]
        if seq==current:
            level=doc['level'] if status=='trusted' else 'unknown'
            db.execute('UPDATE forest_block SET current_risk=?,risk_version=risk_version+1 WHERE id=?',(level,bid))
            log(db,'risk',bid,level,status,doc['id'])
            from .access import recalc_access
            recalc_access(db,bid,doc['id'])
        return {'id':doc['id'],'status':status,'current_risk':db.execute('SELECT current_risk FROM forest_block WHERE id=?',(bid,)).fetchone()[0]}
