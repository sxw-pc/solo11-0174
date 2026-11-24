import argparse,json
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .lidar import import_plot,import_return
from .stand_volume import import_datum,import_control,import_stem_plot,import_survey,latest_assessment
from .store import report
from .audit import audit

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    for name in ('station-import','station-revoke','risk-import','sample-import','shift-import','patrol-import','access-import','access-recheck','field-challenge','plot-import','return-import','datum-import','control-import','stem-plot-import','survey-import'):
        c=s.add_parser(name);c.add_argument('--db',required=True);c.add_argument('--file',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True)
    a=s.add_parser('audit');a.add_argument('--db',required=True)
    t=s.add_parser('stand');t.add_argument('--db',required=True);t.add_argument('--block',required=True)
    x=p.parse_args();fn={'station-import':import_station,'station-revoke':revoke_station,'risk-import':import_risk,'sample-import':import_sample,'shift-import':import_shift,'patrol-import':import_patrol,'access-import':import_access,'access-recheck':recheck_access,'field-challenge':import_field_challenge,'plot-import':import_plot,'return-import':import_return,'datum-import':import_datum,'control-import':import_control,'stem-plot-import':import_stem_plot,'survey-import':import_survey}.get(x.cmd)
    out=fn(x.db,x.file) if fn else (audit(x.db) if x.cmd=='audit' else (latest_assessment(x.db,x.block) if x.cmd=='stand' else report(x.db)))
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
