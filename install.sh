#!/usr/bin/env bash
# Installs burner on an Ubuntu 24.04 host with KVM. Creates no VM and registers no runner.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
[[ -e /dev/kvm ]] || { echo '/dev/kvm is missing: enable virtualization in the firmware (or nested virtualization in the parent hypervisor).' >&2; exit 1; }
cd "$(dirname "$0")"
apt-get update
apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils libvirt-daemon-system \
  libvirt-clients cloud-image-utils nftables python3 openssl curl gpgv ubuntu-cloudimage-keyring tmux btop
getent group burner >/dev/null || groupadd --system burner
install -d -m 0755 /opt/burner
install -m 0755 burner.py /opt/burner/burner.py
ln -sf /opt/burner/burner.py /usr/local/bin/burner
install -m 0644 systemd/* /etc/systemd/system/
install -d -m 0750 /etc/burner
install -d -m 0700 /etc/burner/apps
[[ -e /etc/burner/burner.toml ]] || install -m 0640 burner.example.toml /etc/burner/burner.toml
systemctl daemon-reload
cat <<'TXT'
Done. Next:
  1. edit /etc/burner/burner.toml and copy each GitHub App key to /etc/burner/apps/ (root, 0600)
  2. sudo burner install && sudo burner check
  3. sudo burner base          # builds and self-tests the image (15-40 min)
  4. sudo burner activate
Operators who only watch: sudo usermod -aG burner,systemd-journal <user>, then `burner monitor`.
TXT
