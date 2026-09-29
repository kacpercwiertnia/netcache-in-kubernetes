#!/usr/bin/env python3
"""Run the final small, medium or large matrix; unknown flags go to run-suite."""
import argparse
import subprocess
import sys
from environment import ROOT

parser=argparse.ArgumentParser(description=__doc__,epilog='Pass --limit 1 for one case; --case NAME selects a case. Each case normally redeploys the lab.')
parser.add_argument('size',choices=['small','medium','large'])
parser.add_argument('--list',action='store_true',help='List cases without contacting the cluster')
args,extra=parser.parse_known_args()
cases=ROOT/'presets'/f'{args.size}.json'
if args.list:
    import json
    for case in json.loads(cases.read_text())['cases']:
        print(case['name'])
else:
    timeout='900' if args.size=='small' else '1800'
    settle={'small':'90','medium':'120','large':'180'}[args.size]
    subprocess.run([sys.executable,str(ROOT/'scripts/netcache_experiment_matrix.py'),'run-suite',
        '--cases',str(cases),'--results',str(ROOT/'.generated/experiments'/args.size),
        '--request-batch-size','4','--request-batch-delay','10' if args.size!='medium' else '12',
        '--timeout',timeout,'--settle-between',settle,'--resume','--keep-going',
        '--retries','3','--retry-delay','180',*extra],cwd=ROOT,check=True)
