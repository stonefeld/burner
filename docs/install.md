# Install

About 30 minutes, most of it waiting for the base image to build.

## What you need

| Thing | Minimum |
|---|---|
| OS | Ubuntu 24.04 |
| Virtualization | KVM (`/dev/kvm` exists) |
| CPU and RAM | 4 threads, 16 GiB for one slot of 2 vCPU / 8 GiB |
| Disk | ~10 GiB for the base + each repo's cache (60 GiB cap by default) |
| GitHub | Admin on the repo, to create a GitHub App |

Check KVM:

```bash
ls -l /dev/kvm
```

> [!TIP]
> On a Windows machine, follow the [Hyper-V guide](hyper-v.md) first, then come back here.

## 1. Install the package

```bash
git clone https://github.com/stonefeld/burner.git
cd burner
sudo ./install.sh
```

This installs QEMU, libvirt, nftables and tmux, puts burner in `/opt/burner`, adds the systemd
units and creates the `burner` group. It does not start any VM.

## 2. Create the GitHub App

In the account that owns the repo, go to **Settings → Developer settings → GitHub Apps → New GitHub App**.

| Field | Value |
|---|---|
| Homepage URL | anything |
| Webhook | **unchecked** (burner polls, nothing comes in) |
| Administration | Read and write (registers and removes runners) |
| Actions | Read-only (reads the queue) |
| Variables | Read and write, **only** if you use the heartbeat fallback |
| Everything else | No access |

Then:

1. Create the App and note its **App ID**.
2. **Install** it on that one repo only.
3. Note the **installation ID**: the number at the end of the installation page URL.
4. **Generate a private key**. A `.pem` file downloads.

## 3. Copy the key to the host

From your computer:

```bash
scp app.pem myhost:/tmp/app.pem
```

On the host:

```bash
sudo install -m 0600 -o root -g root /tmp/app.pem /etc/burner/apps/myrepo.pem
rm /tmp/app.pem
```

> [!WARNING]
> Never paste the key into a chat, an issue or a commit. burner refuses keys that are not owned by root
> with mode 0600.

## 4. Configure

```bash
sudoedit /etc/burner/burner.toml
```

The minimum is one repo:

```toml
[[repos]]
name = "owner/repo"
app_id = 123456
installation_id = 78901234
key = "/etc/burner/apps/myrepo.pem"
```

Everything else has a default. [burner.example.toml](../burner.example.toml) lists every option with
comments. The ones you are most likely to touch:

| Option | Default | Change it when |
|---|---|---|
| `slots.count` | 1 | You have RAM for more VMs at once |
| `vm.memory_gib` | 8 | Your tests need more memory |
| `vm.packages` | build tools | Your CI needs system packages |
| `vm.provision` | none | You need a setup script (e.g. browser deps) |
| `network.block` | none | You have public servers jobs must not reach |
| `repos.trusted_branches` | `["main"]` | Your default branch has another name |

## 5. Set up the host

```bash
sudo burner install
```

Loads the firewall, creates one isolated network per slot and starts the status service.

## 6. Check GitHub access

```bash
sudo burner check
```

You should see one `github-ok` line per repo.

## 7. Build the base image

```bash
sudo burner base
```

Takes 15 to 40 minutes. It downloads Ubuntu, installs everything, then **tests isolation** on two copies
before publishing. If the test fails, the image is not published.

## 8. Start

```bash
sudo burner activate
```

## 9. Try it

Add a workflow to the repo:

```yaml
name: burner smoke test
on: workflow_dispatch
jobs:
  hello:
    runs-on: [self-hosted, burner]
    steps:
      - uses: actions/checkout@v4
      - run: docker run --rm hello-world
```

Run it from the Actions tab, and watch:

```bash
burner monitor
```

## Next

- Give teammates read-only access: [monitoring](monitoring.md).
- Make the cache fill and add a fallback: [workflows](workflows.md).
