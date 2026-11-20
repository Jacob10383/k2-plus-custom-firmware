# Install

Unload any filament from the printhead before starting.

!!! warning "K2 Pro"
    K2 Pro support is new and has had far less testing than the K2 Plus.
    Creality firmware stays installed, so you can `swap` back at any time.

SSH into the printer and run:

```sh
python3 -c "import urllib.request; exec(urllib.request.urlopen('https://github.com/Jacob10383/k2-plus-custom-firmware/releases/latest/download/install.py').read(), {'__name__':'__main__'})"
```

The installer reports whether the install succeeded or where it failed,
identified only by a hash of the printer's hardware IDs. Add `--no-telemetry`
to the end of the command to turn this off. The firmware itself has no
telemetry.

Power cycle the printer when instructed.

If Jacobean's firmware was installed before, the installer asks whether to keep
your setup, reset system files or start over. See
[Updates & Recovery](updates-recovery.md).

## First boot

After the printer starts again, connect it to the network:

- Ethernet: nothing else is required.
- Wi-Fi: if Creality firmware was connected to Wi-Fi, the printer joins the
  same network. Otherwise, use the printer screen only far enough to join the
  network.

If you use a Cartographer, it should be plugged in. Then SSH into the printer as `root`
(password `creality_2024`) and run:

```sh
bootstrap
```

Change the password with `passwd` if you so choose.

When bootstrap finishes, Fluidd is available on port `4408` and Mainsail on
port `4409` at the printer's IP address.

See [Probe modes](calibration.md#probe-modes) for the mode bootstrap picks
and the mount and Y-endstop spacer each mode expects.

## Before your first print

1. [Calibrate the printer](calibration.md). Follow the path that applies to
   your probe mode.
2. [Set up OrcaSlicer](setup.md) after calibration.
3. Read [CFS and RFID](cfs.md) to find the Fluidd CFS widget, its controls,
   and RFID setup.
