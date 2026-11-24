# The host talks to guests only through the QEMU guest agent

The host needs to run a few commands in each VM (wait for cloud-init, hand over the registration token, start the runner). Doing it through the QEMU guest agent instead of SSH lets the firewall drop every packet from a guest to the host, removes sshd and host keys from the base, and keeps the token out of the network. The token goes through `virsh` stdin, never argv, so other host processes cannot read it from `/proc`.

## Consequences

There is no interactive console into a job VM; `burner exec` covers debugging, and guest output is treated as untrusted text (sanitized before printing, never parsed as instructions).
