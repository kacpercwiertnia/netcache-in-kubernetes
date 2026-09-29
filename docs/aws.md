# Existing MicroK8s on AWS

Requires an existing, joined MicroK8s cluster on AWS.

1. Copy `inventory/aws.example.yml` to `inventory/hosts.yml`.
2. Set each node's reachable IP, SSH user and private key path. The sample uses Ubuntu accounts.
3. Verify SSH and sudo on every node, including the control plane. The user must also be a member of the `microk8s` group (start a new login session after changing groups).
4. Ensure the AWS network permits your SSH connection and existing MicroK8s inter-node/pod networking. Nodes must be able to fetch images and dependencies.
5. Follow the setup, P4 build, preparation and deployment steps in the main README.

For bastion access, configure `ansible_ssh_common_args` as in [AGH](agh.md).
