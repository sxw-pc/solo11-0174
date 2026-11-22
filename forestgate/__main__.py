import argparse,json
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .store import report
from .audit import audit
from .spectra import import_signature
from .scan import import_scan
from .unmix import import_mixture, import_band4, import_clipping

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    for name in ('station-import','station-revoke','risk-import','sample-import','shift-import','patrol-import','access-import','access-recheck','field-challenge','signature-import','fuel-scan','mixture-import','band4-import','clipping-import'):
        c=s.add_parser(name);c.add_argument('--db',required=True);c.add_argument('--file',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True)
    a=s.add_parser('audit');a.add_argument('--db',required=True)
    x=p.parse_args();fn={'station-import':import_station,'station-revoke':revoke_station,'risk-import':import_risk,'sample-import':import_sample,'shift-import':import_shift,'patrol-import':import_patrol,'access-import':import_access,'access-recheck':recheck_access,'field-challenge':import_field_challenge,'signature-import':import_signature,'fuel-scan':import_scan,'mixture-import':import_mixture,'band4-import':import_band4,'clipping-import':import_clipping}.get(x.cmd)
    print(json.dumps(fn(x.db,x.file) if fn else (audit(x.db) if x.cmd=='audit' else report(x.db)),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
