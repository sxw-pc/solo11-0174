import json,subprocess,sys
from pathlib import Path
import pytest
from forestgate.risk import import_risk as _import_risk,import_station,revoke_station
from forestgate.patrol import import_patrol
from forestgate.access import import_access,recheck_access
from forestgate.store import report,connect
from forestgate.audit import audit

def fixture(tmp,name,doc):
    file=tmp/name;file.write_text(json.dumps(doc),encoding='utf8');return file

def import_risk(db,file):
    station=fixture(file.parent,'station.json',{'id':'station-1','kind':'station','cert_id':'S1-C1','block_id':'B1','station':'S1','valid_from':'2026-01-01T00:00:00Z','valid_to':'2027-01-01T00:00:00Z'})
    import_station(db,station)
    return _import_risk(db,file)

def risk(id='r1',level='low',seq=1,**changes):
    result=dict(id=id,kind='risk',block_id='B1',area='north',station='S1',cert_id='S1-C1',observed_at='2026-11-01T08:00:00Z',seq=seq,level=level,station_valid=True,dry_a=35,dry_b=36,sample_a='A',sample_b='B');result.update(changes);return result

def patrol(id,kind,event='F1',**changes):
    result=dict(id=id,kind='patrol',block_id='B1',area='north',event_id=event,action=kind,actor='guard-a',witness='guard-b',at='2026-11-01T10:00:00Z');result.update(changes);return result

def access(id='a1',**changes):
    result=dict(id=id,kind='access',request_id='P1',block_id='B1',work_type='cold',route='north-road',start_at='2026-11-02T08:00:00Z',end_at='2026-11-02T09:00:00Z',mitigation=False,actor='crew');result.update(changes);return result

def test_risk_changes_access_and_keeps_history(tmp_path):
    print(__import__('forestgate').__file__)
    db=tmp_path/'x.db'
    assert import_risk(db,fixture(tmp_path,'low.json',risk()))['current_risk']=='low'
    assert import_access(db,fixture(tmp_path,'permit.json',access()))['status']=='granted'
    assert import_risk(db,fixture(tmp_path,'high.json',risk(id='r2',seq=2,level='high',dry_a=70,dry_b=72)))['current_risk']=='high'
    assert report(db)['access'][0]['status']=='suspended'
    assert [x['status'] for x in report(db)['decisions'] if x['system']=='access']==['granted','suspended']

def test_unknown_risk_and_source_conflict_rollback(tmp_path):
    db=tmp_path/'x.db';bad=risk(station_valid=False)
    assert import_risk(db,fixture(tmp_path,'bad.json',bad))['current_risk']=='unknown'
    assert import_access(db,fixture(tmp_path,'permit.json',access()))['status']=='held'
    with pytest.raises(ValueError,match='source_id_conflict'):
        import_risk(db,fixture(tmp_path,'changed.json',risk(level='high')))
    assert report(db)['blocks'][0]['current_risk']=='unknown'

def test_claimed_level_must_match_independent_dry_samples(tmp_path):
    db=tmp_path/'x.db'
    result=import_risk(db,fixture(tmp_path,'mismatch.json',risk(level='high',dry_a=35,dry_b=36)))
    assert result['status']=='level_mismatch' and result['current_risk']=='unknown'

def test_hot_work_requires_mitigation(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'low.json',risk()))
    assert import_access(db,fixture(tmp_path,'hot.json',access(work_type='hot')))['reason']=='mitigation_required'

def test_duplicate_risk_revision_rejected_atomically(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'low.json',risk()))
    with pytest.raises(ValueError,match='risk_revision_conflict'):
        import_risk(db,fixture(tmp_path,'other.json',risk(id='r2',level='high')))
    with connect(db) as con:assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0]==2

def test_public_cli(tmp_path):
    db=tmp_path/'cli.db';file=fixture(tmp_path,'low.json',risk())
    station=fixture(tmp_path,'station.json',{'id':'station-1','kind':'station','cert_id':'S1-C1','block_id':'B1','station':'S1','valid_from':'2026-01-01T00:00:00Z','valid_to':'2027-01-01T00:00:00Z'})
    prior=subprocess.run([sys.executable,'-m','forestgate','station-import','--db',str(db),'--file',str(station)],capture_output=True,text=True)
    assert prior.returncode==0,prior.stderr
    cmd=[sys.executable,'-m','forestgate','risk-import','--db',str(db),'--file',str(file)]
    run=subprocess.run(cmd,capture_output=True,text=True)
    assert run.returncode==0,run.stderr
    assert json.loads(run.stdout)['current_risk']=='low'

def test_independent_fire_witness_suspends_and_close_restores_access(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    import_access(db,fixture(tmp_path,'access.json',access()))
    assert import_patrol(db,fixture(tmp_path,'sighting.json',patrol('p1','sighting')))['status']=='active'
    assert report(db)['access'][0]['reason']=='active_fire'
    assert import_patrol(db,fixture(tmp_path,'close.json',patrol('p2','close',actor='guard-c',witness='guard-d',at='2026-11-01T12:00:00Z')))['status']=='closed'
    assert report(db)['access'][0]['status']=='granted'
    assert audit(db)['status']=='matched'

def test_fire_witness_not_independent_rolls_back(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    with pytest.raises(ValueError,match='witness_not_independent'):
        import_patrol(db,fixture(tmp_path,'bad.json',patrol('p1','sighting',witness='guard-a')))
    with connect(db) as con:assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0]==2

def test_station_revocation_reopens_risk_and_holds_permit(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    import_access(db,fixture(tmp_path,'access.json',access()))
    result=revoke_station(db,fixture(tmp_path,'revoke.json',{'id':'station-revoke-1','kind':'station_revoke','cert_id':'S1-C1'}))
    assert result['status']=='revoked'
    assert report(db)['blocks'][0]['current_risk']=='unknown'
    assert report(db)['access'][0]['status']=='held'
    assert audit(db)['status']=='matched'

def test_reopen_fire_after_close_resuspends_existing_permit(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    import_access(db,fixture(tmp_path,'access.json',access()))
    import_patrol(db,fixture(tmp_path,'sighting.json',patrol('p1','sighting')))
    import_patrol(db,fixture(tmp_path,'close.json',patrol('p2','close',actor='guard-c',witness='guard-d',at='2026-11-01T12:00:00Z')))
    import_patrol(db,fixture(tmp_path,'reopen.json',patrol('p3','reopen',at='2026-11-01T13:00:00Z')))
    assert report(db)['access'][0]['status']=='suspended'
    assert report(db)['fires'][0]['version']==3

def test_risk_downgrade_requires_independent_recheck(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'high.json',risk(level='high',dry_a=70,dry_b=72)))
    assert import_access(db,fixture(tmp_path,'access.json',access()))['status']=='suspended'
    import_risk(db,fixture(tmp_path,'low.json',risk(id='r2',seq=2,level='low')))
    assert report(db)['access'][0]['reason']=='recheck_after_risk_downgrade'
    with pytest.raises(ValueError,match='self_recheck'):
        recheck_access(db,fixture(tmp_path,'self.json',{'id':'check-1','kind':'recheck','request_id':'P1','actor':'crew','checked_at':'2026-11-03T00:00:00Z'}))
    assert recheck_access(db,fixture(tmp_path,'check.json',{'id':'check-2','kind':'recheck','request_id':'P1','actor':'guard-c','checked_at':'2026-11-03T00:00:00Z'}))['status']=='granted'
    assert audit(db)['status']=='matched'

def test_fire_location_and_temporal_close_checked(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    with pytest.raises(ValueError,match='fire_outside_block'):
        import_patrol(db,fixture(tmp_path,'wrong.json',patrol('p0','sighting',area='south')))
    import_patrol(db,fixture(tmp_path,'sighting.json',patrol('p1','sighting')))
    with pytest.raises(ValueError,match='fire_evidence_order_or_independence'):
        import_patrol(db,fixture(tmp_path,'early.json',patrol('p2','close',actor='guard-c',witness='guard-d',at='2026-11-01T09:00:00Z')))
    assert len(report(db)['fires'])==1
