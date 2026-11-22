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
CREATE TABLE IF NOT EXISTS nursery_plot(id TEXT PRIMARY KEY,block_id TEXT NOT NULL,area_m2 REAL NOT NULL,source_id TEXT NOT NULL REFERENCES source(id));
CREATE TABLE IF NOT EXISTS soil_reading(source_id TEXT PRIMARY KEY REFERENCES source(id),plot_id TEXT NOT NULL REFERENCES nursery_plot(id),at TEXT NOT NULL,capacitance REAL NOT NULL);
CREATE TABLE IF NOT EXISTS irrigation_advice(plot_id TEXT PRIMARY KEY,liters REAL NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calibration_batch(batch_id TEXT PRIMARY KEY,offset_min REAL NOT NULL,offset_max REAL NOT NULL,salt_curve TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES source(id));
CREATE TABLE IF NOT EXISTS probe(probe_id TEXT PRIMARY KEY,plot_id TEXT NOT NULL REFERENCES nursery_plot(id),frequency_hz REAL NOT NULL,temperature_c REAL NOT NULL,batch_id TEXT NOT NULL REFERENCES calibration_batch(batch_id),salt_curve TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES source(id));
CREATE TABLE IF NOT EXISTS water_tank(tank_id TEXT PRIMARY KEY,capacity_liters REAL NOT NULL,remaining_liters REAL NOT NULL,served_plots TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES source(id));
CREATE TABLE IF NOT EXISTS dual_frequency_scan(source_id TEXT PRIMARY KEY REFERENCES source(id),scan_id TEXT NOT NULL,actor TEXT NOT NULL,observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dual_frequency_cell(source_id TEXT NOT NULL REFERENCES source(id),plot_id TEXT NOT NULL,reading_source_id TEXT NOT NULL REFERENCES soil_reading(source_id),cell_id TEXT NOT NULL,area_m2 REAL NOT NULL,response_d REAL,covered INTEGER NOT NULL,PRIMARY KEY(source_id,cell_id));
CREATE TABLE IF NOT EXISTS soil_sample(source_id TEXT PRIMARY KEY REFERENCES source(id),sample_id TEXT NOT NULL,plot_id TEXT NOT NULL REFERENCES nursery_plot(id),cell_id TEXT NOT NULL UNIQUE,area_m2 REAL NOT NULL,bounds_m2 TEXT NOT NULL,water_pct REAL NOT NULL,actor TEXT NOT NULL,observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS moisture_plan(plan_id TEXT NOT NULL,version INTEGER NOT NULL,tank_id TEXT NOT NULL,tank_remaining_liters REAL NOT NULL,total_scheduled_liters REAL NOT NULL,max_state_demand_liters REAL NOT NULL,tank_shortfall_liters REAL NOT NULL,status TEXT NOT NULL,evidence_source_ids TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(plan_id,version));
CREATE TABLE IF NOT EXISTS plan_plot(plan_id TEXT NOT NULL,version INTEGER NOT NULL,plot_id TEXT NOT NULL,area_m2 REAL NOT NULL,w_min REAL,w_max REAL,demand_min_liters REAL NOT NULL,demand_max_liters REAL NOT NULL,safe_liters REAL NOT NULL,scheduled_liters REAL NOT NULL,confirmed_area_m2 REAL NOT NULL,pending_area_m2 REAL NOT NULL,status TEXT NOT NULL,evidence_source_ids TEXT NOT NULL,PRIMARY KEY(plan_id,version,plot_id));
CREATE TABLE IF NOT EXISTS plan_cell(plan_id TEXT NOT NULL,version INTEGER NOT NULL,plot_id TEXT NOT NULL,cell_id TEXT NOT NULL,area_m2 REAL NOT NULL,coverage TEXT NOT NULL,w_min REAL,w_max REAL,safe_liters REAL NOT NULL,scheduled_liters REAL NOT NULL,status TEXT NOT NULL,evidence_source_id TEXT,PRIMARY KEY(plan_id,version,plot_id,cell_id));
CREATE TABLE IF NOT EXISTS valve_action(plan_id TEXT NOT NULL,version INTEGER NOT NULL,plot_id TEXT NOT NULL,cell_id TEXT NOT NULL,liters REAL NOT NULL,classification TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(plan_id,version,plot_id,cell_id));
CREATE TABLE IF NOT EXISTS plan_joint_state(plan_id TEXT NOT NULL,version INTEGER NOT NULL,state_seq INTEGER NOT NULL,label TEXT NOT NULL,offsets TEXT NOT NULL,plot_demands TEXT NOT NULL,total_demand_liters REAL NOT NULL,fits_tank INTEGER NOT NULL,PRIMARY KEY(plan_id,version,state_seq));
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
          'nursery_plots':'SELECT * FROM nursery_plot ORDER BY id','soil_readings':'SELECT * FROM soil_reading ORDER BY source_id',
          'irrigation_advice':'SELECT * FROM irrigation_advice ORDER BY plot_id',
          'calibration_batches':'SELECT * FROM calibration_batch ORDER BY batch_id','probes':'SELECT * FROM probe ORDER BY probe_id',
          'water_tanks':'SELECT * FROM water_tank ORDER BY tank_id','dual_frequency_scans':'SELECT * FROM dual_frequency_scan ORDER BY source_id',
          'dual_frequency_cells':'SELECT * FROM dual_frequency_cell ORDER BY source_id,cell_id',
          'soil_samples':'SELECT * FROM soil_sample ORDER BY source_id',
          'moisture_plans':'SELECT * FROM moisture_plan ORDER BY plan_id,version','plan_plots':'SELECT * FROM plan_plot ORDER BY plan_id,version,plot_id',
          'plan_cells':'SELECT * FROM plan_cell ORDER BY plan_id,version,plot_id,cell_id',
          'valve_actions':'SELECT * FROM valve_action ORDER BY plan_id,version,plot_id,cell_id',
          'plan_joint_states':'SELECT * FROM plan_joint_state ORDER BY plan_id,version,state_seq',
          'decisions':'SELECT * FROM decision_log ORDER BY id'}.items()}
