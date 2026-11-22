import json,subprocess,sys
from pathlib import Path
import pytest
from forestgate.risk import import_risk as _import_risk,import_station,revoke_station,import_sample
from forestgate.patrol import import_patrol as _import_patrol,import_shift
from forestgate.access import import_access,recheck_access,import_field_challenge
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

def import_patrol(db,file):
    doc=json.loads(file.read_text(encoding='utf8'))
    for actor in {doc['actor'],doc['witness']}:
        shift=fixture(file.parent,f'shift-{actor}.json',{'id':f'shift-src-{actor}','kind':'shift','shift_id':f'shift-{actor}','block_id':doc['block_id'],'actor':actor,'team':actor,'start_at':'2026-11-01T00:00:00Z','end_at':'2026-11-02T00:00:00Z'})
        import_shift(db,shift)
    return _import_patrol(db,file)

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

def test_same_revision_conflict_freezes_then_new_revision_resolves(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'low.json',risk()))
    import_access(db,fixture(tmp_path,'access.json',access()))
    conflict=import_risk(db,fixture(tmp_path,'other.json',risk(id='r2',level='high')))
    assert conflict['status']=='revision_conflict' and conflict['current_risk']=='unknown'
    assert report(db)['access'][0]['status']=='held'
    assert report(db)['conflicts'][0]['opposes_source_id']=='r1'
    assert import_risk(db,fixture(tmp_path,'new.json',risk(id='r3',seq=2)))['current_risk']=='low'
    assert report(db)['access'][0]['status']=='granted'
    assert audit(db)['status']=='matched'

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
    with connect(db) as con:assert con.execute('SELECT COUNT(*) FROM source').fetchone()[0]==3

def test_station_revocation_reopens_risk_and_holds_permit(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    import_access(db,fixture(tmp_path,'access.json',access()))
    result=revoke_station(db,fixture(tmp_path,'revoke.json',{'id':'station-revoke-1','kind':'station_revoke','cert_id':'S1-C1'}))
    assert result['status']=='revoked'
    assert report(db)['blocks'][0]['current_risk']=='unknown'
    assert report(db)['access'][0]['status']=='held'
    assert audit(db)['status']=='matched'

def test_two_independent_dry_samples_change_access_decision(tmp_path):
    db=tmp_path/'x.db'
    pending=import_risk(db,fixture(tmp_path,'risk.json',risk(split_samples=True,dry_a=None,dry_b=None)))
    assert pending['status']=='pending_samples' and pending['current_risk']=='unknown'
    assert import_access(db,fixture(tmp_path,'permit.json',access()))['status']=='held'
    a={'id':'sample-1','kind':'sample','risk_source_id':'r1','plot_id':'plot-A','actor':'observer-a','dry':35,'observed_at':'2026-11-01T08:00:00Z'}
    b={'id':'sample-2','kind':'sample','risk_source_id':'r1','plot_id':'plot-B','actor':'observer-b','dry':36,'observed_at':'2026-11-01T08:01:00Z'}
    assert import_sample(db,fixture(tmp_path,'sample-a.json',a))['status']=='pending_second_sample'
    with pytest.raises(ValueError,match='sample_not_independent'):
        import_sample(db,fixture(tmp_path,'duplicate-actor.json',{**b,'id':'bad-sample','actor':'observer-a'}))
    assert import_sample(db,fixture(tmp_path,'sample-b.json',b))['current_risk']=='low'
    assert report(db)['access'][0]['status']=='granted'
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


def test_revoking_unused_station_does_not_freeze_current_risk(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    import_access(db,fixture(tmp_path,'permit.json',access()))
    extra={'id':'station-2','kind':'station','cert_id':'S1-C2','block_id':'B1','station':'S1','valid_from':'2026-01-01T00:00:00Z','valid_to':'2027-01-01T00:00:00Z'}
    import_station(db,fixture(tmp_path,'extra.json',extra))
    revoke_station(db,fixture(tmp_path,'revoke.json',{'id':'revoke-2','kind':'station_revoke','cert_id':'S1-C2'}))
    assert report(db)['blocks'][0]['current_risk']=='low'
    assert report(db)['access'][0]['status']=='granted'
    assert audit(db)['status']=='matched'


def test_late_sample_and_revoked_station_cannot_validate_split_risk(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk(split_samples=True,dry_a=None,dry_b=None)))
    sample={'id':'sample-1','kind':'sample','risk_source_id':'r1','plot_id':'plot-A','actor':'observer-a','dry':35,'observed_at':'2026-10-31T08:00:00Z'}
    with pytest.raises(ValueError,match='sample_predates_risk'):
        import_sample(db,fixture(tmp_path,'early.json',sample))
    revoke_station(db,fixture(tmp_path,'revoke.json',{'id':'revoke-1','kind':'station_revoke','cert_id':'S1-C1'}))
    with pytest.raises(ValueError,match='sample_station_not_qualified'):
        import_sample(db,fixture(tmp_path,'after.json',{**sample,'observed_at':'2026-11-01T08:02:00Z'}))
    assert report(db)['blocks'][0]['current_risk']=='unknown'
    assert report(db)['samples']==[]


def test_patrol_shift_team_and_ambiguity_are_real_evidence(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    p=fixture(tmp_path,'patrol.json',patrol('p1','sighting'))
    with pytest.raises(ValueError,match='missing_or_ambiguous_patrol_shift'):_import_patrol(db,p)
    for actor in ('guard-a','guard-b'):
        import_shift(db,fixture(tmp_path,f'{actor}.json',{'id':f'src-{actor}','kind':'shift','shift_id':f'shift-{actor}','block_id':'B1','actor':actor,'team':'team-1','start_at':'2026-11-01T00:00:00Z','end_at':'2026-11-02T00:00:00Z'}))
    with pytest.raises(ValueError,match='witness_same_team'):_import_patrol(db,p)
    assert report(db)['fires']==[]


def test_recheck_only_after_downgrade_and_later_than_observation(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'high.json',risk(level='high',dry_a=70,dry_b=72)))
    import_access(db,fixture(tmp_path,'permit.json',access()))
    check={'id':'check-1','kind':'recheck','request_id':'P1','actor':'guard-c','checked_at':'2026-11-01T07:00:00Z'}
    with pytest.raises(ValueError,match='recheck_not_due'):
        recheck_access(db,fixture(tmp_path,'premature.json',check))
    import_risk(db,fixture(tmp_path,'low.json',risk(id='r2',seq=2,level='low')))
    with pytest.raises(ValueError,match='recheck_predates_risk'):
        recheck_access(db,fixture(tmp_path,'early.json',check))
    assert report(db)['access'][0]['status']=='held'
    assert audit(db)['status']=='matched'


def test_two_independent_route_observations_challenge_risk_until_new_certified_version(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    import_access(db,fixture(tmp_path,'permit.json',access()))
    first={'id':'field-1','kind':'field_challenge','challenge_id':'C1','request_id':'P1','risk_source_id':'r1','actor':'observer-a','plot_id':'plot-a','dry':78,'observed_at':'2026-11-01T09:00:00Z'}
    second={**first,'id':'field-2','actor':'observer-b','plot_id':'plot-b','dry':79,'observed_at':'2026-11-01T09:01:00Z'}
    assert import_field_challenge(db,fixture(tmp_path,'field-a.json',first))['status']=='pending'
    assert report(db)['access'][0]['status']=='granted'
    with pytest.raises(ValueError,match='challenge_conflict_or_not_independent'):
        import_field_challenge(db,fixture(tmp_path,'bad.json',{**second,'id':'bad-field','actor':'observer-a'}))
    assert import_field_challenge(db,fixture(tmp_path,'field-b.json',second))['status']=='confirmed_discrepancy'
    assert report(db)['blocks'][0]['current_risk']=='unknown'
    assert report(db)['access'][0]['status']=='held'
    assert import_risk(db,fixture(tmp_path,'new.json',risk(id='r2',seq=2,level='high',dry_a=78,dry_b=79)))['current_risk']=='high'
    assert report(db)['access'][0]['status']=='suspended'
    assert audit(db)['status']=='matched'


def test_hot_work_mitigation_changes_permit_conclusion(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    assert import_access(db,fixture(tmp_path,'no-plan.json',access(work_type='hot')))['status']=='held'
    assert import_access(db,fixture(tmp_path,'with-plan.json',access(id='a2',request_id='P2',work_type='hot',mitigation=True)))['status']=='granted'
    assert audit(db)['status']=='matched'


def test_route_must_reach_declared_block(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    with pytest.raises(ValueError,match='route_outside_block'):
        import_access(db,fixture(tmp_path,'wrong-route.json',access(route='south-road')))
    assert report(db)['access']==[]


def test_reopen_requires_prior_independent_close(tmp_path):
    db=tmp_path/'x.db';import_risk(db,fixture(tmp_path,'risk.json',risk()))
    import_patrol(db,fixture(tmp_path,'sighting.json',patrol('p1','sighting')))
    with pytest.raises(ValueError,match='fire_not_closed'):
        import_patrol(db,fixture(tmp_path,'reopen.json',patrol('p2','reopen',at='2026-11-01T11:00:00Z')))
    assert report(db)['fires'][0]['status']=='active'
