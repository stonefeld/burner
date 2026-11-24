# Operations

## Day to day

### See what is going on

```bash
burner status
```

```bash
burner monitor
```

### Stop taking jobs

Running jobs finish normally:

```bash
sudo burner pause
```

Cut running jobs too:

```bash
sudo burner pause --now
```

### Resume

```bash
sudo burner activate
```

### Build a new base now

Needed after changing `vm.packages` or `vm.provision`. It also runs every Sunday on its own.

```bash
sudo burner base
```

### Test isolation again

Safe while jobs run: it uses a separate maintenance network.

```bash
sudo burner selftest
```

### Throw away a repo's cache

The next trusted job fills it again.

```bash
sudo burner cache-reset owner/repo
```

### Look inside a running VM

The number is the slot. Output is sanitized before it reaches your terminal.

```bash
sudo burner exec 1 -- df -h /cache
```

### Read the full log

```bash
journalctl -u 'burner-*' -f
```

## Changing the configuration

| You changed | Then |
|---|---|
| `[cache.env]`, `[[repos]]`, `vm.memory_gib`, `vm.vcpus` | nothing, applies to the next job |
| `vm.packages`, `vm.provision`, `vm.ubuntu` | [build a new base](#build-a-new-base-now) |
| `[network]`, `slots.count` | run the three commands below |

```bash
sudo burner pause --now
sudo burner install
sudo burner activate
```

## Troubleshooting

### Jobs stay queued

Check the header of `burner status`:

| You see | Do |
|---|---|
| `PAUSED` | [resume](#resume) |
| `Filter MISMATCH` | [reload the filter](#the-loaded-filter-does-not-match-the-installed-one) |
| `Base none` | [build a base](#build-a-new-base-now) |
| Snapshot in red | [restart the status service](#the-snapshot-is-old) |

Then look at the **jobs** window of `burner monitor`.

### "The loaded filter does not match the installed one"

Something else changed nftables, or you edited the config. Reload and look at what changed:

```bash
sudo systemctl restart burner-firewall
sudo nft list table inet burner
```

### The snapshot is old

The dashboard header turns red when the status service stopped:

```bash
sudo systemctl restart burner-snapshot
```

### "WAN observers disagree"

The host cannot confirm its public IP, so no VM starts. Check internet access from the host:

```bash
curl -4 https://api.ipify.org
```

### Every job starts with an empty cache

The cache is only written by successful jobs on a trusted branch (push, schedule or manual run). If your
CI only runs on pull requests, it never fills. See [workflows](workflows.md#fill-the-cache).

### "Preparing the base failed"

The end of the cloud-init log is in the journal. It is usually your `vm.provision` script:

```bash
journalctl -u burner-base -n 120
```

### A job runs out of memory

Raise `vm.memory_gib` in the config. It applies to the next VM, no rebuild needed.

## Where things live

| Path | What |
|---|---|
| `/etc/burner/burner.toml` | configuration |
| `/etc/burner/apps/` | GitHub App keys (root only) |
| `/etc/burner/firewall.nft` | generated firewall, do not edit |
| `/var/lib/burner/` | slot state, expected policy, WAN, last self-test |
| `/run/burner/status.json` | snapshot for the dashboard |
| `/var/lib/libvirt/images/burner/` | base images, caches, running slots |
