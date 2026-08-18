# Install

Unload any filament from the printhead before starting.

SSH into the printer and run:

```sh
python3 -c "import urllib.request; exec(urllib.request.urlopen('https://firmware.jacobean.xyz/install.py').read(), {'__name__':'__main__'})"
```

Power cycle the printer when instructed. If SSH drops during the final archive
step before the completion message appears, the install is complete; power
cycle the printer.

## First boot

After the printer starts again, connect it to the network:

- Ethernet: nothing else is required.
- Wi-Fi: use the printer screen only far enough to join the network.

Then SSH into the printer and run:

```sh
bootstrap
```

When bootstrap finishes, Fluidd is available on port `4408` and Mainsail on
port `4409` at the printer's IP address.

The default configuration expects the 5.8 mm Y-endstop spacer and the
Cartographer mount used by K2 Improvements.

## Before your first print

1. [Calibrate the printer](calibration.md). Follow the path that applies to
   your probe mode.
2. [Set up OrcaSlicer](setup.md) after calibration.
3. Read [CFS and RFID](cfs.md) to find the Fluidd CFS widget, its controls,
   and RFID setup.
