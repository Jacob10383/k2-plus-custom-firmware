# K2 Plus and K2 Pro Custom Firmware

A complete alternative firmware stack for the Creality K2 Plus and K2 Pro.

It replaces the stock software environment with a custom Linux kernel, root
filesystem, and [Kalico fork](https://github.com/Jacob10383/kalico). The
firmware includes ground-up implementations of CFS control, closed-loop motor
control, power-loss recovery, and other K2-specific systems.

This is a release repository containing the assembled firmware files and user
documentation.

This is an independent project and is not affiliated with Creality.

## Repository layout

- `install.py` — installation entrypoint
- `bootstrap` and `swap` — printer setup and firmware-slot commands
- `index` — release metadata, including the SHA-256 of each firmware image
- `extras/` — Kalico extensions for K2-specific hardware and functionality
- `docs/` — user documentation

The firmware images, `rootfs.ext2` and `kernel.img`, are attached to each
[release](https://github.com/Jacob10383/k2-plus-custom-firmware/releases)
instead of being stored in the repository.

For installation, setup, calibration, OrcaSlicer configuration, updates, and
reference material, see the
[documentation](https://jacob10383.github.io/k2-plus-custom-firmware/).
