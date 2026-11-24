# Workflows

## Use burner

```yaml
jobs:
  test:
    runs-on: [self-hosted, burner]
```

burner picks up a job when:

- its labels are `self-hosted` + `burner` (plus `linux` / `x64` if you want);
- it comes from the repo itself, never a fork;
- it is in the `workflows` list, if you set one.

> [!NOTE]
> Labels are not a security boundary. Any workflow in the repo can ask for them. That is why burner asks
> GitHub which job really ran before trusting its cache.

## Fill the cache

Only successful jobs on a trusted branch write the cache. A workflow that only runs on pull requests
never fills it. Add a push trigger:

```yaml
on:
  pull_request:
  push:
    branches: [main]
```

Or keep a small "warm the cache" workflow that only installs and builds, once a day:

```yaml
on:
  schedule:
    - cron: "0 5 * * *"
  workflow_dispatch:
```

## Fall back to GitHub when the host is off

1. Add a heartbeat variable to the repo in `burner.toml`:

   ```toml
   [[repos]]
   name = "owner/repo"
   heartbeat = "BURNER_HEARTBEAT"
   ```

2. Give the App **Variables: Read and write**.

3. Add a `pick` job that chooses the runner:

   ```yaml
   jobs:
     pick:
       runs-on: ubuntu-latest
       outputs:
         runner: ${{ steps.r.outputs.runner }}
       steps:
         - id: r
           env:
             BEAT: ${{ vars.BURNER_HEARTBEAT }}
             FORK: ${{ github.event.pull_request.head.repo.fork }}
           run: |
             age=$(( $(date +%s) - ${BEAT:-0} ))
             if [ "$FORK" != true ] && [ "$age" -lt 300 ]; then
               echo 'runner=["self-hosted","burner"]'
             else
               echo 'runner="ubuntu-latest"'
             fi | tee -a "$GITHUB_OUTPUT"

     test:
       needs: pick
       runs-on: ${{ fromJSON(needs.pick.outputs.runner) }}
   ```

How it behaves:

| Host state | Jobs go to |
|---|---|
| active and healthy | burner |
| paused, off, or firewall broken (for 5 min) | `ubuntu-latest` |
| PR from a fork | `ubuntu-latest` |

> [!WARNING]
> The `pick` job itself runs on GitHub and uses one minute per run. If your account has no GitHub
> minutes at all, skip the fallback and use `runs-on: [self-hosted, burner]` directly.

## Adjust steps for each environment

`runner.environment` is `self-hosted` on burner and `github-hosted` on GitHub.

### GitHub's cache: only on GitHub

On burner it would upload over your connection what is already on disk.

```yaml
- uses: actions/cache@v4
  if: runner.environment == 'github-hosted'
```

### Docker builds: local cache on burner

```yaml
- uses: docker/setup-buildx-action@v3
  with:
    driver: ${{ runner.environment == 'self-hosted' && 'docker' || 'docker-container' }}
- uses: docker/build-push-action@v6
  with:
    cache-from: ${{ runner.environment == 'github-hosted' && 'type=gha' || '' }}
    cache-to: ${{ runner.environment == 'github-hosted' && 'type=gha,mode=max' || '' }}
```

### Playwright: system deps live in the base

The runner has no sudo, so install system deps with `vm.provision` (example below).

```yaml
- run: npx playwright install ${{ runner.environment == 'github-hosted' && '--with-deps' || '' }} chromium
```

### Parallel tests: bound by memory

```yaml
- run: pytest -n ${{ runner.environment == 'self-hosted' && '4' || 'auto' }}
```

### Quick reference

| Thing | On burner |
|---|---|
| `setup-uv` | leave `enable-cache` unset: it uses the disk cache automatically |
| `setup-node` / `setup-python` `cache:` input | only on GitHub, like `actions/cache` |
| Fixed ports in `services:` | fine, every job has its own VM |
| `sudo apt install` in a step | move it to `vm.packages` or `vm.provision` |

## Example `vm.provision`

Save as `/etc/burner/provision.sh` and set `provision = "/etc/burner/provision.sh"` under `[vm]`:

```bash
#!/bin/bash
# Runs as root while building the base.
set -euxo pipefail

# System libraries your build needs, e.g. a database client and fonts.
apt-get install -y --no-install-recommends libpq-dev fonts-liberation

# Chromium system dependencies for your repo's Playwright version.
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs
npx -y playwright@1.56.0 install-deps chromium
```

Then rebuild:

```bash
sudo burner base
```

## Avoid

- `pull_request_target` with a checkout of the PR: it runs foreign code with your secrets.
- Production secrets in jobs that run on pull requests: the VM can reach the internet.
- Relying on files left between jobs: nothing survives, except the cache.
