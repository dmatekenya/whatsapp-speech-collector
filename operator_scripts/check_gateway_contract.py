"""Run INSIDE the approved gateway container, no database write performed."""
import os
import urllib.request
import urllib.error
import json

secret=os.environ.get('SUPERVISION_REVIEW_TOKEN','')
read_secret=os.environ.get('SUPERVISION_GATEWAY_TOKEN','')
base='http://127.0.0.1:8875'
if not secret or not read_secret:
    raise SystemExit('Gateway tokens missing; do not print their values')
for path, token in [('/v1/supervision/health',read_secret),
                    ('/v1/supervision/write-status',secret),
                    ('/v1/supervision/snapshot',read_secret),
                    ('/v1/supervision/revision',read_secret)]:
    req=urllib.request.Request(base+path,headers={'Authorization':'Bearer '+token})
    try:
        with urllib.request.urlopen(req,timeout=15) as resp:
            data=json.loads(resp.read())
            assert resp.status == 200
            if path.endswith('write-status'):
                assert data.get('mode')=='human-review'
                assert data.get('collector_qc_mutation') is False
            if path.endswith('snapshot'):
                assert data['snapshot'].get('human_review_available') is True
                assert len(data['recordings'])<=10000
            print(path, 'OK')
    except (urllib.error.HTTPError,urllib.error.URLError,KeyError,ValueError,AssertionError) as exc:
        print(path, 'NOT READY',type(exc).__name__)
        raise SystemExit(1)
print('Contract verified. No review was submitted and no recording was modified.')
