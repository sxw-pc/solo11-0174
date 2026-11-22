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
CREATE TABLE IF NOT EXISTS wildlife_camera(id TEXT PRIMARY KEY,block_id TEXT NOT NULL,x_km REAL NOT NULL,y_km REAL NOT NULL,coverage_km REAL NOT NULL DEFAULT 0.05,source_id TEXT NOT NULL REFERENCES source(id));
CREATE TABLE IF NOT EXISTS wildlife_frame(source_id TEXT PRIMARY KEY REFERENCES source(id),camera_id TEXT NOT NULL REFERENCES wildlife_camera(id),species TEXT NOT NULL,track_hint TEXT NOT NULL,at TEXT NOT NULL,quality REAL NOT NULL,session TEXT NOT NULL DEFAULT 'default');
CREATE TABLE IF NOT EXISTS wildlife_count(block_id TEXT NOT NULL,species TEXT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(block_id,species));
CREATE TABLE IF NOT EXISTS wildlife_clock(source_id TEXT PRIMARY KEY REFERENCES source(id),camera_id TEXT NOT NULL REFERENCES wildlife_camera(id),session TEXT NOT NULL,offset_lo_s REAL NOT NULL,offset_hi_s REAL NOT NULL,UNIQUE(camera_id,session));
CREATE TABLE IF NOT EXISTS wildlife_gap(source_id TEXT PRIMARY KEY REFERENCES source(id),camera_id TEXT NOT NULL REFERENCES wildlife_camera(id),start_at TEXT NOT NULL,end_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wildlife_media(source_id TEXT PRIMARY KEY REFERENCES source(id),frame_id TEXT NOT NULL REFERENCES wildlife_frame(source_id),media_kind TEXT NOT NULL,sha TEXT NOT NULL,shot_at TEXT NOT NULL,mark TEXT,archive_ref TEXT);
CREATE TABLE IF NOT EXISTS wildlife_cosighting(source_id TEXT NOT NULL REFERENCES source(id),frame_id TEXT NOT NULL REFERENCES wildlife_frame(source_id),cameras TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,PRIMARY KEY(source_id,frame_id));
CREATE TABLE IF NOT EXISTS wildlife_assessment(source_id TEXT PRIMARY KEY REFERENCES source(id),block_id TEXT NOT NULL,species TEXT NOT NULL,season TEXT NOT NULL,version INTEGER NOT NULL,lower_count INTEGER NOT NULL,upper_count INTEGER NOT NULL,lower_assignment TEXT NOT NULL,upper_assignment TEXT NOT NULL,evidence TEXT NOT NULL,UNIQUE(block_id,species,season,version));
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
          'wildlife_cameras':'SELECT * FROM wildlife_camera ORDER BY id','wildlife_frames':'SELECT * FROM wildlife_frame ORDER BY source_id',
          'wildlife_counts':'SELECT * FROM wildlife_count ORDER BY block_id,species',
          'wildlife_clocks':'SELECT * FROM wildlife_clock ORDER BY source_id','wildlife_gaps':'SELECT * FROM wildlife_gap ORDER BY source_id',
          'wildlife_media':'SELECT * FROM wildlife_media ORDER BY source_id','wildlife_cosightings':'SELECT * FROM wildlife_cosighting ORDER BY source_id,frame_id',
          'wildlife_assessments':'SELECT * FROM wildlife_assessment ORDER BY block_id,species,season,version',
          'decisions':'SELECT * FROM decision_log ORDER BY id'}.items()}
