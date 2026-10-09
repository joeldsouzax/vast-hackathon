"""Internal inspection, capability checks, and generic result-artifact worker."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

from foundation_records import FoundationSettings, ChunkManifest, AnalysisWindow, SearchQuery
from foundation_providers import Registry
from foundation_storage import FileStorage, inspect_video
from foundation import Foundation
from recording_hook import atomic_json


def worker(window, manifests, settings, storage):
    """Same provider boundary as coordinator jobs; remote work never opens SQLite."""
    from foundation_worker import analyze_artifacts
    from foundation_providers import bounded_call
    registry=Registry(settings)
    return asyncio.run(bounded_call(lambda:analyze_artifacts(window,manifests,settings,storage,registry),
                                    window.deadline_utc,settings.limits))


def main(command):
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    if command=='foundation-worker':
        parser.add_argument('--request',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
        parser.add_argument('--storage',type=Path,required=True)
    if command=='foundation-query':
        parser.add_argument('--runtime',type=Path,required=True);parser.add_argument('--run-id',required=True);parser.add_argument('--text',required=True)
    args=parser.parse_args(sys.argv[2:])
    settings=FoundationSettings.load(args.config)
    if command=='foundation-capabilities':
        result=Registry(settings).capabilities();print(json.dumps(result,indent=2))
        return 0 if all(r['ready'] for r in result.values()) else 1
    if command=='foundation-worker':
        request=json.loads(args.request.read_text())
        if set(request)!={'window','manifests'}:raise ValueError('Invalid worker request fields')
        window=AnalysisWindow.model_validate(request['window'])
        manifests=[ChunkManifest.model_validate(m) for m in request['manifests']]
        results=worker(window,manifests,settings,FileStorage(args.storage))
        atomic_json(args.output,{'window':window.model_dump(),'results':[r.model_dump() for r in results], 'origin':'fixture'})
        return 0
    f=Foundation(args.runtime,args.run_id,settings,readonly=True)
    try:
        print(json.dumps(f.search(SearchQuery(event_id=settings.event.event_id,run_id=args.run_id,text=args.text)),indent=2))
    finally:f.close()
    return 0
