# Existing MicroK8s on AGH servers

Requires an existing, joined MicroK8s cluster.

1. Connect to the network/VPN required to reach your bastion.
2. Copy `inventory/agh.example.yml` to `inventory/hosts.yml`.
3. Replace the example bastion, login, private node IPs, node user and SSH key with your assigned values.
4. Verify `ssh -J YOUR_LOGIN@BASTION NODE_USER@CONTROL_IP` and passwordless sudo / `microk8s` access.
5. Follow the same README setup and deployment steps as on AWS.

For interactive bastion authentication, open a persistent SSH connection:

```bash
mkdir -p ~/.ssh/control
ssh -M -N -f -S ~/.ssh/control/lab-bastion YOUR_LOGIN@BASTION
```

```yaml
ansible_ssh_common_args: '-o ProxyCommand="ssh -S ~/.ssh/control/lab-bastion -W %h:%p YOUR_LOGIN@BASTION"'
```

Ansible and the runner use this connection. Node login must be non-interactive. Close the connection with `ssh -S ~/.ssh/control/lab-bastion -O exit YOUR_LOGIN@BASTION`.
