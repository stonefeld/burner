# burner

Ephemeral GitHub Actions runners: one throwaway KVM VM per job, with a per-repo cache only trusted jobs write. User docs start at `README.md`; vocabulary is in `GLOSSARY.md`.

## Before changing behaviour

Read `docs/adr/`: each file records a decision that looks odd without its reason (no SSH into guests, polling instead of webhooks, a single stdlib-only file). A change that contradicts one needs a new ADR that supersedes it.

## Invariants

- Every barrier lives on the host. Guest output is untrusted text: sanitize it with `printable()` before printing and decide outcomes from the GitHub API.
- Secrets travel on stdin, never in argv, logs, events or the status snapshot.
- Watching stays unprivileged: anything the dashboard shows comes from `/run/burner/status.json` or the journal.
- The cache is promoted only through `trusted()`.

## Checks

```bash
python3 -m unittest test_burner
```

Unit tests cover the pure logic. Anything touching KVM, nftables or GitHub is only proven on a real host with `sudo burner selftest` and a test workflow; say so when a change could not be validated there.

## Docs

User docs follow one style: short sections, numbered steps, every command in its own fenced block, tables for options and symptoms, GitHub alerts (`> [!NOTE]`) for caveats.
