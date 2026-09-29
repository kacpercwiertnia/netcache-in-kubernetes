#!/usr/bin/env python3
"""Fetch pinned external code without modifying existing dirty checkouts."""
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def git(path,*args):
    return subprocess.check_output(['git','-C',str(path),*args],text=True).strip()

def main():
    for name,dep in json.loads((ROOT/'dependencies.json').read_text()).items():
        path=ROOT/'.deps'/name
        if (path/'.git').is_dir():
            exclude=path/'.git/info/exclude'
            existing=exclude.read_text() if exclude.exists() else ''
            for pattern in ('/data/', '__pycache__/', '*.pyc'):
                if pattern not in existing.splitlines():
                    existing+='\n'+pattern+'\n'
            exclude.write_text(existing)
        if path.exists():
            if not (path/'.git').exists():
                raise RuntimeError(f'{path} exists and is not a Git checkout')
            if git(path,'status','--porcelain','--untracked-files=normal'):
                raise RuntimeError(f'{path} has local changes; preserve them before updating dependencies')
            if git(path,'rev-parse','HEAD')==dep['commit']:
                print(f'{name}: already at {dep["commit"]}')
                continue
        else:
            path.mkdir(parents=True)
            git(path,'init')
            git(path,'remote','add','origin',dep['url'])
        git(path,'fetch','--depth','1',dep['url'],dep['commit'])
        git(path,'checkout','--detach',dep['commit'])
        exclude=path/'.git/info/exclude'
        with exclude.open('a') as handle:
            handle.write('\n/data/\n__pycache__/\n*.pyc\n')
        assert git(path,'rev-parse','HEAD')==dep['commit']
        print(f'{name}: {dep["commit"]}')

if __name__=='__main__':
    main()
