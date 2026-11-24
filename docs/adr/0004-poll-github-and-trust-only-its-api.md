# Poll GitHub from the host and trust only its API

burner polls the Actions API every 20 s with a GitHub App installation token instead of receiving `workflow_job` webhooks, so the host needs no inbound port. Decisions about a job's outcome come from the API, never from the guest: a compromised guest can print anything. Runner labels do not tie a job to a runner (any queued job with matching labels can take it), so before promoting a cache burner asks GitHub which job actually ran on that runner.

## Considered Options

- **Webhooks**: lower latency, but needs a public endpoint into the host.
- **Personal access token**: broader scope and tied to a person; an App is scoped to one repo with minimal permissions.
