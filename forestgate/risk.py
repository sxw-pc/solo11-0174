from .store import connect,log,source
from contextlib import closing

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
        valid=int(bool(doc['station_valid']))
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
