import argparse,json
from .risk import import_risk,import_station,revoke_station,import_sample
from .patrol import import_patrol,import_shift
from .access import import_access,recheck_access,import_field_challenge
from .wildlife import import_camera,import_frame
from .individuals import import_clock,import_gap,import_media,import_cosighting,publish_assessment
from .store import report
from .audit import audit

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    for name in ('station-import','station-revoke','risk-import','sample-import','shift-import','patrol-import','access-import','access-recheck','field-challenge',
                 'wildlife-camera-import','wildlife-frame-import','wildlife-clock-import','wildlife-gap-import','wildlife-media-import','wildlife-cosighting-import','wildlife-assessment-publish'):
        c=s.add_parser(name);c.add_argument('--db',required=True);c.add_argument('--file',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True)
    a=s.add_parser('audit');a.add_argument('--db',required=True)
    x=p.parse_args();fn={'station-import':import_station,'station-revoke':revoke_station,'risk-import':import_risk,'sample-import':import_sample,'shift-import':import_shift,'patrol-import':import_patrol,'access-import':import_access,'access-recheck':recheck_access,'field-challenge':import_field_challenge,
                         'wildlife-camera-import':import_camera,'wildlife-frame-import':import_frame,'wildlife-clock-import':import_clock,'wildlife-gap-import':import_gap,'wildlife-media-import':import_media,'wildlife-cosighting-import':import_cosighting,'wildlife-assessment-publish':publish_assessment}.get(x.cmd)
    print(json.dumps(fn(x.db,x.file) if fn else (audit(x.db) if x.cmd=='audit' else report(x.db)),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
