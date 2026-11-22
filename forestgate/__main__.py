import argparse,json
from .risk import import_risk,import_station,revoke_station
from .patrol import import_patrol
from .access import import_access,recheck_access
from .store import report
from .audit import audit

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    for name in ('station-import','station-revoke','risk-import','patrol-import','access-import','access-recheck'):
        c=s.add_parser(name);c.add_argument('--db',required=True);c.add_argument('--file',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True)
    a=s.add_parser('audit');a.add_argument('--db',required=True)
    x=p.parse_args();fn={'station-import':import_station,'station-revoke':revoke_station,'risk-import':import_risk,'patrol-import':import_patrol,'access-import':import_access,'access-recheck':recheck_access}.get(x.cmd)
    print(json.dumps(fn(x.db,x.file) if fn else (audit(x.db) if x.cmd=='audit' else report(x.db)),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
