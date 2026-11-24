# The cache is a per-repo disk that only trusted jobs write

Throwaway VMs boot cold, so caching decides how fast burner is. Each repo has a cache disk image mounted copy-on-write into every job VM; after a job, burner flattens that VM's overlay into the new cache only if GitHub reports the job as a successful `push`/`schedule`/`workflow_dispatch` run on a `trusted_branches` branch of the repo itself. Pull requests get the full cache but their writes are discarded, so the cache is exactly as trustworthy as the trusted branches.

## Considered Options

- **Persistent cache shared by every job**: any pull request could poison it.
- **Pull-through proxies on the host** (registry mirror, package proxy): opens a host port to the guests and lets untrusted jobs write into a shared store. The disk approach opens nothing, and the host never mounts the guest filesystem (it only copies qcow2 files).

## Consequences

Workflows that only run on `pull_request` never fill the cache; the docs push users to add a trusted trigger.
