# burner

Throwaway VMs that run GitHub Actions jobs on a machine you own, with a cache that only trusted jobs write.

## Language

### Machines

**Host**:
The machine that runs burner and KVM; physical, or a VM with nested virtualization.
_Avoid_: server, hypervisor

**Slot**:
One place where a job VM can run, with its own network. Slots are numbered from 1.
_Avoid_: runner, worker

**Maintenance slot**:
Slot 0, used only by the self-test and the base build, never by jobs.

**Job VM**:
The VM a slot boots for exactly one job and destroys afterwards.
_Avoid_: guest (fine in code), runner VM

### Images

**Base**:
The sealed, read-only image every job VM boots from: Ubuntu, Docker, the runner and the configured packages.
_Avoid_: template, golden image

**Repo cache**:
A per-repo disk image with everything jobs downloaded and built, mounted copy-on-write into each job VM.
_Avoid_: shared cache, volume

**Promotion**:
Replacing the repo cache with what a trusted job left behind.

### Trust

**Trusted job**:
A successful job from a push, schedule or manual run on a trusted branch of the repo itself. The only kind that can cause a promotion.

**Trusted branch**:
A branch whose code already passed review, listed in `trusted_branches`.

**Self-test**:
The isolation and disposal check run on the maintenance slot before a base is published.

### Watching

**Snapshot**:
The status file the root service writes so people can watch without sudo.

**Heartbeat**:
A repo variable burner refreshes while healthy, so workflows can fall back to GitHub-hosted runners.
