"""Resolve the existing MicroK8s control plane from Ansible inventory."""
from functools import lru_cache
import json
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def inventory_path():
    return Path(os.environ.get('INVENTORY', ROOT / 'inventory' / 'hosts.yml')).expanduser().resolve()


@lru_cache(maxsize=1)
def control_host():
    path = inventory_path()
    if not path.is_file():
        raise RuntimeError(f'Inventory missing: {path}. Copy and edit inventory/aws.example.yml or agh.example.yml.')
    data = json.loads(subprocess.check_output(['ansible-inventory', '-i', str(path), '--list'], text=True))
    def hosts(group):
        obj = data.get(group, {})
        return obj.get('hosts', []) + [h for child in obj.get('children', []) for h in hosts(child)]
    names = list(dict.fromkeys(hosts('control')))
    if len(names) != 1:
        raise RuntimeError('Inventory must define exactly one host in the control group.')
    name = names[0]
    variables = data.get('_meta', {}).get('hostvars', {}).get(name, {})
    if not variables.get('ansible_user'):
        raise RuntimeError('Set ansible_user for the control host in inventory.')
    return name, variables


def control_plane_ip():
    name, variables = control_host()
    return str(variables.get('ansible_host', name))


def ssh_target():
    return f'{control_host()[1]["ansible_user"]}@{control_plane_ip()}'


def remote_home():
    variables = control_host()[1]
    return variables.get('remote_home', f'/home/{variables["ansible_user"]}')


def ssh_cmd(remote, *, input_text=None, check=True, timeout=None):
    variables = control_host()[1]
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'LogLevel=ERROR']
    if variables.get('ansible_port'):
        cmd += ['-p', str(variables['ansible_port'])]
    if variables.get('ansible_ssh_private_key_file'):
        cmd += ['-i', os.path.expanduser(variables['ansible_ssh_private_key_file'])]
    for key in ('ansible_ssh_common_args', 'ansible_ssh_extra_args'):
        cmd += shlex.split(variables.get(key, ''))
    cmd += [ssh_target(), 'bash -lc ' + shlex.quote(remote)]
    return subprocess.run(cmd, input=input_text, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check, timeout=timeout, cwd=ROOT)


def metrics_mode():
    mode = os.environ.get('NETCACHE_METRICS_MODE', 'cluster')
    if mode not in ('cluster', 'ssh'):
        raise ValueError('NETCACHE_METRICS_MODE must be cluster or ssh')
    return mode
