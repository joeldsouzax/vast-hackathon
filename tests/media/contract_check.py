"""Release unit-test media allocations before sustained acceptance checks."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]


class CoverageResult(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.rows=[]
    def addSuccess(self,test):super().addSuccess(test);self.rows.append({'test':test.id(),'passed':True})
    def addFailure(self,test,error):super().addFailure(test,error);self.rows.append({'test':test.id(),'passed':False,'reason':self._exc_info_to_string(error,test)})
    def addError(self,test,error):super().addError(test,error);self.rows.append({'test':test.id(),'passed':False,'reason':self._exc_info_to_string(error,test)})
    def addSkip(self,test,reason):super().addSkip(test,reason);self.rows.append({'test':test.id(),'passed':False,'reason':'Skipped: '+reason})


def run_units(folder,log_name='unit.log'):
    folder=Path(folder);log=folder/log_name;report=folder/'unit-results.json'
    command=[sys.executable,str(Path(__file__).resolve()),'--log',str(log),'--report',str(report)]
    with log.open('w') as output:
        process=subprocess.run(command,stdout=output,stderr=subprocess.STDOUT,timeout=300)
    result=json.loads(report.read_text())
    if process.returncode:result['passed']=False
    result['isolated_process']=True
    return result


def run_preparations(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with (folder/'measurement.log').open('w') as output:
        process=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--preparations',str(folder)],
            stdout=output,stderr=subprocess.STDOUT,timeout=1000)
    result=json.loads((folder/'report.json').read_text())
    if process.returncode:result['target_passed']=False
    result['isolated_process']=True
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--log',type=Path);parser.add_argument('--report',type=Path)
    parser.add_argument('--preparations',type=Path)
    args=parser.parse_args()
    sys.path.insert(0,str(ROOT/'app'));sys.path.insert(0,str(ROOT/'tests/media'))
    os.environ['PYDANTIC_AI_NO_BANNER']='1'
    import pydantic_ai.models
    pydantic_ai.models.ALLOW_MODEL_REQUESTS=False
    original=socket.socket.connect
    def isolated(sock,address):
        if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
            raise AssertionError('External fixture traffic')
        return original(sock,address)
    socket.socket.connect=isolated
    if args.preparations:
        from replay_check import preparation_measurements
        result=preparation_measurements(args.preparations,20)
        return 0 if result['target_passed'] else 1
    if not args.log or not args.report:parser.error('Unit checks require log and report paths')
    result=unittest.TextTestRunner(stream=sys.stdout,resultclass=CoverageResult,verbosity=2).run(
        unittest.defaultTestLoader.discover(str(ROOT/'tests/unit')))
    passed=result.wasSuccessful() and not result.skipped and not result.expectedFailures
    args.report.write_text(json.dumps({'count':result.testsRun,'passed':passed,'results':result.rows,'log':str(args.log)},indent=2)+'\n')
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
