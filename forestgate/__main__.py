import argparse,json,os
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .store import report
from .audit import audit
from .hotspot import import_tile
from .mosaic import build as mosaic_build,publish as mosaic_publish,recover as mosaic_recover,verify as mosaic_verify

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    for name in ('station-import','station-revoke','risk-import','sample-import','shift-import','patrol-import','access-import','access-recheck','field-challenge','tile-import'):
        c=s.add_parser(name);c.add_argument('--db',required=True);c.add_argument('--file',required=True)
    for name in ('mosaic-build','mosaic-publish'):
        c=s.add_parser(name);c.add_argument('--db',required=True);c.add_argument('--file',required=True);c.add_argument('--out',required=True)
    c=s.add_parser('mosaic-recover');c.add_argument('--db',required=True);c.add_argument('--out',required=True)
    c=s.add_parser('mosaic-verify');c.add_argument('--db',required=True);c.add_argument('--out')
    r=s.add_parser('report');r.add_argument('--db',required=True)
    a=s.add_parser('audit');a.add_argument('--db',required=True)
    x=p.parse_args()
    if x.cmd=='mosaic-build':out=mosaic_build(x.db,x.file,x.out)
    elif x.cmd=='mosaic-publish':
        point=os.environ.get('FORESTGATE_FAIL_AT') or None
        out=mosaic_publish(x.db,x.file,x.out,fail_at=point,hard=bool(point))
    elif x.cmd=='mosaic-recover':out=mosaic_recover(x.db,x.out)
    elif x.cmd=='mosaic-verify':out=mosaic_verify(x.db,x.out)
    else:
        fn={'station-import':import_station,'station-revoke':revoke_station,'risk-import':import_risk,'sample-import':import_sample,'shift-import':import_shift,'patrol-import':import_patrol,'access-import':import_access,'access-recheck':recheck_access,'field-challenge':import_field_challenge,'tile-import':import_tile}.get(x.cmd)
        out=fn(x.db,x.file) if fn else (audit(x.db) if x.cmd=='audit' else report(x.db))
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
