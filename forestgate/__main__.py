import argparse,json
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .soil import (import_plot,import_reading,import_calibration,import_probe,
                   import_tank,import_dual_frequency_scan,import_soil_sample)
from .moisture_bounds import build_plan
from .store import report
from .audit import audit

IMPORTERS={'station-import':import_station,'station-revoke':revoke_station,'risk-import':import_risk,
           'sample-import':import_sample,'shift-import':import_shift,'patrol-import':import_patrol,
           'access-import':import_access,'access-recheck':recheck_access,'field-challenge':import_field_challenge,
           'plot-import':import_plot,'reading-import':import_reading,'calibration-import':import_calibration,
           'probe-import':import_probe,'tank-import':import_tank,'scan-import':import_dual_frequency_scan,
           'soil-sample-import':import_soil_sample}

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    for name in IMPORTERS:
        c=s.add_parser(name);c.add_argument('--db',required=True);c.add_argument('--file',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True)
    a=s.add_parser('audit');a.add_argument('--db',required=True)
    m=s.add_parser('moisture-plan');m.add_argument('--db',required=True);m.add_argument('--plan-id',required=True)
    m.add_argument('--tank-id');
    x=p.parse_args()
    if x.cmd in IMPORTERS:
        out=IMPORTERS[x.cmd](x.db,x.file)
    elif x.cmd=='audit':
        out=audit(x.db)
    elif x.cmd=='moisture-plan':
        out=build_plan(x.db,x.plan_id,x.tank_id)
    else:
        out=report(x.db)
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
