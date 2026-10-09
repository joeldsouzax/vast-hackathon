"""MediaMTX 1.20.1 completion hook. Atomic sidecars survive missed delivery."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.request


def atomic_json(path, data):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.hook-')
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(data,out);out.flush();os.fsync(out.fileno())
        os.replace(temp,path)
    finally:Path(temp).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['create','complete']);parser.add_argument('--port',type=int,required=True)
    args=parser.parse_args()
    path=Path(os.environ['MTX_SEGMENT_PATH']).resolve()
    if os.environ['MTX_PATH']=='program':return
    payload={'stage':args.stage,'path':str(path),'source_path':os.environ['MTX_PATH'],'utc':time.time()}
    if args.stage=='complete':
        # Completion is evidence of closure; file size stability is never used.
        payload['duration_s']=float(os.environ['MTX_SEGMENT_DURATION'])
        atomic_json(path.with_suffix('.complete'),payload)
        return
    request=urllib.request.Request(f'http://127.0.0.1:{args.port}/recording',json.dumps(payload).encode(),
                                   {'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(request,timeout=5) as response:owner=json.load(response)
    atomic_json(path.with_suffix('.owner'),owner)


if __name__=='__main__':main()
