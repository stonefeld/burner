# Isolation

**Assume every job is malicious.** Inside its VM a job can become root (it has Docker). So every barrier
lives on the host, where the VM cannot touch it.

## What is blocked

| From a job to | Blocked by |
|---|---|
| The host itself | firewall: nothing from the VM reaches the host |
| Your LAN, router, VPN | firewall: all private ranges, Tailscale, link-local |
| Your router through your public IP | firewall: your public IP, re-checked every 5 min |
| Your own public servers | firewall: `network.block` |
| Another job VM | one network per slot |
| IPv6, spoofed addresses | libvirt L2 filter |
| Host files, devices, sockets | none attached, checked on every boot |
| Credentials | only a single-use registration token enters the VM |
| The next job | disks are thrown away |

What is allowed out:

| Allowed | Why |
|---|---|
| TCP 80 and 443 to anywhere | package registries and CDNs change addresses |
| DNS to the configured resolvers | name resolution |
| NTP | clock |

> [!WARNING]
> Because 443 is open, a job can send out whatever it has: the repo code and any secret the workflow gives
> it. Keep production secrets away from jobs that run pull requests.

## Checks before every VM boots

1. The firewall loaded in the kernel matches the one burner installed.
2. The public IP is confirmed by two observers.
3. The L2 filter and the slot network are intact.
4. Nothing but KVM is plugged into the slot's bridge.
5. After boot: one network card, filtered, no host folders or devices.
6. Inside: cloud-init finished, cache and Docker are up, GitHub answers.

If any check fails, the VM does not start.

## The self-test

Runs before every new base is published. Run it yourself any time:

```bash
sudo burner selftest
```

It boots two VMs in a row and checks:

| Check | Pass when |
|---|---|
| Fresh disk | a file written by the first VM is gone in the second |
| Fresh identity | `machine-id` differs |
| Docker | works as the `runner` user |
| Internet | HTTPS to GitHub connects |
| Blocked targets | host ports, the other network, LAN and router addresses, Tailscale, cloud metadata, a closed port on a public IP, your public IP, each `network.block` entry: all **time out** |
| DNS | a resolver that is not allowed gets no answer |
| Base intact | its SHA-256 did not change |

> [!NOTE]
> "Connection refused" counts as a failure: it means the packet got there. Only a timeout proves the block.

## Not covered

- Bugs in KVM/QEMU themselves. When burner runs inside another VM, keep that VM's own network limits as a
  second layer ([Hyper-V guide](hyper-v.md)).
- Your router is not audited; only its public IP and private ranges are blocked.
- If your public IP changes mid-job, there are up to 5 minutes until it is re-checked.
- IPv6 is blocked at L2 but the self-test does not send IPv6 traffic.
- Bandwidth is not capped. CPU, memory and time are.
