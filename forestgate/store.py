import hashlib,json,sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source(id TEXT PRIMARY KEY, kind TEXT NOT NULL, sha TEXT NOT NULL, raw BLOB NOT NULL, imported_seq INTEGER NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS forest_block(id TEXT PRIMARY KEY, area TEXT NOT NULL, station TEXT NOT NULL, current_risk TEXT NOT NULL DEFAULT 'unknown', risk_version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS station_cert(id TEXT PRIMARY KEY,block_id TEXT NOT NULL,station TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,status TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES source(id));
CREATE TABLE IF NOT EXISTS risk_observation(source_id TEXT PRIMARY KEY REFERENCES source(id), block_id TEXT NOT NULL, seq INTEGER NOT NULL, station_valid INTEGER NOT NULL, dry_a INTEGER, dry_b INTEGER, level TEXT NOT NULL, cert_id TEXT, observed_at TEXT NOT NULL, split_samples INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS risk_conflict(source_id TEXT PRIMARY KEY REFERENCES source(id),block_id TEXT NOT NULL,seq INTEGER NOT NULL,opposes_source_id TEXT NOT NULL REFERENCES risk_observation(source_id));
CREATE TABLE IF NOT EXISTS risk_sample(source_id TEXT PRIMARY KEY REFERENCES source(id),risk_source_id TEXT NOT NULL REFERENCES risk_observation(source_id),plot_id TEXT NOT NULL,actor TEXT NOT NULL,dry INTEGER NOT NULL,observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fire_event(source_id TEXT PRIMARY KEY REFERENCES source(id), block_id TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, witness TEXT NOT NULL, at TEXT NOT NULL,segment TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS patrol_shift(id TEXT PRIMARY KEY,block_id TEXT NOT NULL,actor TEXT NOT NULL,team TEXT NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES source(id));
CREATE TABLE IF NOT EXISTS fire_state(event_id TEXT PRIMARY KEY,block_id TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL,reason TEXT NOT NULL,segment TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS access_request(source_id TEXT PRIMARY KEY REFERENCES source(id),request_id TEXT NOT NULL,block_id TEXT NOT NULL,work_type TEXT NOT NULL,route TEXT NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,mitigation INTEGER NOT NULL,actor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS access_recheck(source_id TEXT PRIMARY KEY REFERENCES source(id),request_id TEXT NOT NULL,actor TEXT NOT NULL,checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS field_challenge(source_id TEXT PRIMARY KEY REFERENCES source(id),challenge_id TEXT NOT NULL,request_id TEXT NOT NULL,risk_source_id TEXT NOT NULL REFERENCES risk_observation(source_id),actor TEXT NOT NULL,plot_id TEXT NOT NULL,dry INTEGER NOT NULL,observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS challenge_state(challenge_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,risk_source_id TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS access_state(request_id TEXT PRIMARY KEY,status TEXT NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,system TEXT NOT NULL,object_id TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,source_id TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS lightning_receiver(id TEXT PRIMARY KEY,x_km REAL NOT NULL,y_km REAL NOT NULL,clock_offset_us REAL NOT NULL,source_id TEXT NOT NULL REFERENCES source(id));
CREATE TABLE IF NOT EXISTS lightning_pulse(source_id TEXT PRIMARY KEY REFERENCES source(id),burst_id TEXT,receiver_id TEXT NOT NULL REFERENCES lightning_receiver(id),arrival_us REAL NOT NULL,amplitude REAL NOT NULL,boot_id TEXT,page_seq INTEGER,waveform TEXT);
CREATE TABLE IF NOT EXISTS lightning_locate(burst_id TEXT PRIMARY KEY,x_km REAL NOT NULL,y_km REAL NOT NULL,method TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lightning_site(id TEXT PRIMARY KEY,coord_version TEXT NOT NULL,time_unit TEXT NOT NULL,speed_km_per_us REAL NOT NULL,source_id TEXT NOT NULL REFERENCES source(id));
CREATE TABLE IF NOT EXISTS lightning_clock(source_id TEXT PRIMARY KEY REFERENCES source(id),receiver_id TEXT NOT NULL,boot_id TEXT NOT NULL,clock_source TEXT NOT NULL,offset_us REAL NOT NULL,uncertainty_us REAL NOT NULL,boot_started_at TEXT NOT NULL,evidence TEXT NOT NULL DEFAULT '',version INTEGER NOT NULL,UNIQUE(receiver_id,boot_id,version));
CREATE TABLE IF NOT EXISTS lightning_compartment(id TEXT PRIMARY KEY,block_id TEXT NOT NULL,polygon TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES source(id));
CREATE TABLE IF NOT EXISTS lightning_event(id TEXT PRIMARY KEY,seq INTEGER NOT NULL UNIQUE,status TEXT NOT NULL,receiver_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lightning_event_pulse(event_id TEXT NOT NULL REFERENCES lightning_event(id),pulse_source_id TEXT PRIMARY KEY REFERENCES lightning_pulse(source_id),receiver_id TEXT NOT NULL,boot_id TEXT NOT NULL,arrival_us REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lightning_release(id TEXT PRIMARY KEY,version INTEGER NOT NULL UNIQUE,source_id TEXT NOT NULL REFERENCES source(id),coord_version TEXT NOT NULL,time_unit TEXT NOT NULL,speed_km_per_us REAL NOT NULL,events_total INTEGER NOT NULL,unverified_events INTEGER NOT NULL,pending_compartments TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lightning_release_event(release_id TEXT NOT NULL REFERENCES lightning_release(id),event_id TEXT NOT NULL,status TEXT NOT NULL,event_time TEXT,own_compartments TEXT NOT NULL,pending_compartments TEXT NOT NULL,joint_delta_us TEXT NOT NULL,own_delta_us TEXT NOT NULL,PRIMARY KEY(release_id,event_id));
CREATE TABLE IF NOT EXISTS lightning_release_compartment(release_id TEXT NOT NULL REFERENCES lightning_release(id),event_id TEXT NOT NULL,compartment_id TEXT NOT NULL,block_id TEXT NOT NULL,coverage TEXT NOT NULL,shift_id TEXT,team TEXT,PRIMARY KEY(release_id,event_id,compartment_id));
CREATE TABLE IF NOT EXISTS lightning_release_todo(release_id TEXT NOT NULL REFERENCES lightning_release(id),event_id TEXT NOT NULL,compartment_id TEXT NOT NULL,block_id TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(release_id,event_id,compartment_id));
'''

def connect(path):
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(target);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db

def source(db,file,kind):
    raw=Path(file).read_bytes();doc=json.loads(raw);digest=hashlib.sha256(raw).hexdigest()
    if doc.get('kind')!=kind or not doc.get('id'):raise ValueError('source_kind_or_id')
    old=db.execute('SELECT * FROM source WHERE id=?',(doc['id'],)).fetchone()
    if old:
        if old['sha']!=digest:raise ValueError('source_id_conflict')
        return doc,False
    seq=db.execute('SELECT COALESCE(MAX(imported_seq),0)+1 FROM source').fetchone()[0]
    db.execute('INSERT INTO source VALUES(?,?,?,?,?)',(doc['id'],kind,digest,raw,seq))
    return doc,True

def log(db,system,key,status,reason,source_id):
    db.execute('INSERT INTO decision_log(system,object_id,status,reason,source_id) VALUES(?,?,?,?,?)',(system,key,status,reason,source_id))

def report(db_path):
    with closing(connect(db_path)) as db:
        return {name:[dict(x) for x in db.execute(query)] for name,query in {
          'blocks':'SELECT * FROM forest_block ORDER BY id','stations':'SELECT * FROM station_cert ORDER BY id','risk':'SELECT * FROM risk_observation ORDER BY seq',
          'samples':'SELECT * FROM risk_sample ORDER BY source_id','conflicts':'SELECT * FROM risk_conflict ORDER BY source_id',
          'fires':'SELECT * FROM fire_state ORDER BY event_id','shifts':'SELECT * FROM patrol_shift ORDER BY id','access':'SELECT * FROM access_state ORDER BY request_id',
          'rechecks':'SELECT * FROM access_recheck ORDER BY source_id','challenges':'SELECT * FROM field_challenge ORDER BY source_id','challenge_states':'SELECT * FROM challenge_state ORDER BY challenge_id',
          'lightning_receivers':'SELECT * FROM lightning_receiver ORDER BY id','lightning_pulses':'SELECT * FROM lightning_pulse ORDER BY source_id',
          'lightning_locations':'SELECT * FROM lightning_locate ORDER BY burst_id',
          'lightning_sites':'SELECT * FROM lightning_site ORDER BY id','lightning_clocks':'SELECT * FROM lightning_clock ORDER BY source_id',
          'lightning_compartments':'SELECT * FROM lightning_compartment ORDER BY id','lightning_events':'SELECT * FROM lightning_event ORDER BY seq',
          'lightning_event_pulses':'SELECT * FROM lightning_event_pulse ORDER BY event_id, pulse_source_id',
          'lightning_releases':'SELECT * FROM lightning_release ORDER BY version',
          'lightning_release_events':'SELECT * FROM lightning_release_event ORDER BY release_id, event_id',
          'lightning_release_compartments':'SELECT * FROM lightning_release_compartment ORDER BY release_id, event_id, compartment_id',
          'lightning_release_todos':'SELECT * FROM lightning_release_todo ORDER BY release_id, event_id, compartment_id',
          'decisions':'SELECT * FROM decision_log ORDER BY id'}.items()}
