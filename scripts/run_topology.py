#!/usr/bin/env python3
"""Generate a NetCache/Linux fabric and attach the inner Kubernetes cluster."""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
from environment import ROOT, inventory_path, control_host, metrics_mode


def converter_module():
    path = ROOT / 'ansible/roles/topology_pipeline/files/adj_to_kathara.py'
    spec = importlib.util.spec_from_file_location('topology_converter', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def generate(args, topology_args):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', args.lab):
        raise ValueError('Lab name must contain 1–63 lowercase letters, digits or hyphens.')
    dependency = ROOT / '.deps/EvalNet'
    if not (dependency / 'tool.py').is_file():
        raise RuntimeError('EvalNet missing. Run scripts/setup.sh first.')
    labs = ROOT / '.generated/local-labs'
    labs.mkdir(parents=True, exist_ok=True)
    # Generate in a fresh directory: never select an unrelated cached graph by mtime.
    with tempfile.TemporaryDirectory(prefix='evalnet-', dir=labs) as tmp:
        subprocess.run([sys.executable, str(dependency / 'tool.py'), 'generate', args.topology, *topology_args], cwd=tmp, check=True)
        adjacency = list(Path(tmp).rglob('*.adj.txt'))
        if len(adjacency) != 1:
            raise RuntimeError(f'Expected one generated graph, found {len(adjacency)}.')
        converter = converter_module()
        count, _, _ = converter.parse_adj_file(adjacency[0])
        if args.netcache_nodes is not None:
            nodes = sorted(set(int(s) for s in args.netcache_nodes.split(',')))
            if not nodes or min(nodes) < 0 or max(nodes) >= count:
                raise ValueError(f'NetCache node IDs must be in 0..{count-1}.')
        else:
            if not 0 < args.netcache_percent <= 100:
                raise ValueError('NetCache percentage must be greater than 0 and at most 100.')
            nodes = sorted(random.Random(args.seed).sample(range(count), math.ceil(count * args.netcache_percent / 100)))
        lab = labs / args.lab
        built = Path(tmp) / 'lab'
        converter.generate_lab(
            input_file=adjacency[0], output_dir=built,
            image=args.netcache_image, host_image=args.host_image,
            p4_json='/shared/p4/netcache.json', lab_name=args.lab,
            netcache_nodes=nodes,
            topology_kind=args.topology_kind,
            access_policy=args.access_policy,
            access_nodes=[int(v) for v in args.access_nodes.split(',')] if args.access_nodes else None,
        )
        shutil.copy2(adjacency[0], built / 'topology.adj.txt')
        meta_path = built / 'topology.meta.json'
        meta = json.loads(meta_path.read_text())
        meta['source_file'] = 'topology.adj.txt'
        meta_path.write_text(json.dumps(meta, indent=2) + '\n')
        if lab.exists():
            shutil.rmtree(lab)
        shutil.move(str(built), lab)
    print(f'Generated {lab}: {len(nodes)}/{count} NetCache devices ({100*len(nodes)/count:.2f}%).', flush=True)
    return lab


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('topology', help='EvalNet topology, e.g. fattree, xgft, slimfly, dragonfly, jellyfish')
    parser.add_argument('lab')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--netcache-percent', type=float, default=33)
    selection.add_argument('--netcache-nodes', help='Explicit comma-separated graph node IDs')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--topology-kind', choices=['generic','fattree','spine-leaf'], default='generic')
    parser.add_argument('--access-policy', choices=['auto','all','explicit'], default='auto')
    parser.add_argument('--access-nodes', default='')
    parser.add_argument('--netcache-image', default=os.environ.get('NETCACHE_IMAGE','docker.io/kacpercwiertnia/p4-builder-runtime:v4@sha256:5df33ab6d0ab19457282a81be897b80db113188b00590c39481c7f3a4f0548e5'))
    parser.add_argument('--host-image', default='kathara/base:latest@sha256:8a2e70ac8f51bb283b8549fd686f65f9a1ef4487fe2c81849cb74d301e61970b')
    parser.add_argument('--generate-only', action='store_true', help='Generate locally without contacting any cluster')
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--vars-file', type=Path, help='Extra Ansible YAML variables (or set ENVIRONMENT_VARS)')
    argv=sys.argv[1:]
    separator=argv.index('--') if '--' in argv else len(argv)
    args=parser.parse_args(argv[:separator])
    topology_args=argv[separator+1:]
    if args.inventory:
        os.environ['INVENTORY']=str(args.inventory.resolve())
    mode = metrics_mode()
    if not args.generate_only:
        control_host()
        for file in [ROOT/'.deps/netcache/src/control_plane/controller.py',ROOT/'.generated/p4/netcache.json']:
            if not file.is_file():
                raise RuntimeError(f'Missing {file}; run setup.sh and build_p4.sh.')
    lab=generate(args,topology_args)
    if args.generate_only:
        return
    extra={
        'topology_local_lab_dir':str(lab), 'topology_lab_name':args.lab,
        'netcache_metrics_mode': mode,
    }
    for variable, env in [('kinda_netcache_switch_capacity_entries','KINDA_NETCACHE_SWITCH_CAPACITY_ENTRIES'),('kinda_netcache_switch_capacity_slots','KINDA_NETCACHE_SWITCH_CAPACITY_SLOTS')]:
        if env in os.environ:
            extra[variable]=int(os.environ[env])
    cmd=['ansible-playbook','-i',str(inventory_path()),str(ROOT/'ansible/run_topology.yml')]
    variables=args.vars_file or os.environ.get('ENVIRONMENT_VARS')
    if variables:
        cmd+=['-e','@'+str(Path(variables).expanduser().resolve())]
    cmd+=['-e',json.dumps(extra)]
    subprocess.run(cmd,cwd=ROOT,check=True)


if __name__=='__main__':
    try:
        main()
    except (RuntimeError,ValueError,subprocess.CalledProcessError) as error:
        print(error,file=sys.stderr)
        sys.exit(1)
