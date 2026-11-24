# KVM is the only backend; Windows hosts use nested virtualization

burner drives libvirt/KVM only. On Windows it runs inside a Hyper-V VM with nested virtualization, which reuses the same code and isolation, and that outer VM keeps Hyper-V port ACLs as a second layer. A native Hyper-V backend (differencing VHDX per job, per-VM ACLs) would be faster but means a second controller to maintain; it is only worth building if measurements show nesting is too slow.
