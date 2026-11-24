# One job per throwaway KVM VM

CI jobs run untrusted code (pull requests, dependencies) with Docker, which is root inside wherever it runs. Each job therefore gets a fresh VM booted from qcow2 overlays and destroyed afterwards, with every barrier on the host. Registering the runner as `--ephemeral` alone is not enough: the disk must be replaced between jobs, or one job can persist into the next.

## Considered Options

- **Runner on the host or in a container sharing the host's Docker**: no barrier between the job and the host's network and files.
- **Persistent VM**: fastest and simplest, but a pull request can leave something for the next job, including trusted ones on `main`. Kept only as a documented manual fallback for Hyper-V.
- **Kubernetes / Actions Runner Controller**: disproportionate for one host.
