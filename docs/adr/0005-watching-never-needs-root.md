# Watching never needs root

A dashboard built from privileged tools (nethogs, SSH into guests, nftables counters) ends up asking for sudo inside its panes. A sandboxed root service now writes a status snapshot to `/run/burner/status.json` (root:burner 0640) every 2 s, and `burner status` / `burner monitor` only read that file and the journal. Per-VM traffic comes from the host's `vnet*` counters instead of nethogs. Actions that change state stay root-only.
