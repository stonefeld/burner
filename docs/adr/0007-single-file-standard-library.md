# Single file, Python standard library only

`burner.py` is one file with no dependencies beyond Python 3.11+ on Ubuntu, plus system tools (virsh, nft, qemu-img, cloud-localds, curl, openssl, gpgv). A security tool that runs as root is easier to audit, install and copy to a host when there is no package tree to vet. Guest-side scripts are embedded as strings and travel inside the cloud-init seed.

## Consequences

The GitHub App JWT is signed by calling `openssl` instead of a JWT library. Split the file only when a part grows a real reason to stand alone.
