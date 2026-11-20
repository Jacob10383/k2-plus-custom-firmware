# Updates & Recovery

## Update the firmware stack

Update Klipper from the update manager in Fluidd or Mainsail. The firmware's
Klipper extras are part of [Kalico](https://github.com/Jacob10383/kalico) and
update with it.

Update bootstrap:

```sh
bootstrap --update
```

To update the kernel and system, reinstall as in
[Update Creality firmware](#update-creality-firmware), skipping step 2.

## Update the printer configuration

```sh
bootstrap --update configs
```

This installs the configuration for your printer,
[K2 Plus](https://github.com/Jacob10383/kalico/tree/main/config/k2) or
[K2 Pro](https://github.com/Jacob10383/kalico/tree/main/config/k2-pro), from the
installed Klipper, so update Klipper first to get the latest. It keeps
your `SAVE_CONFIG` block, `overrides.cfg`, files you added and your probe mode.
Every other shipped file, including `moonraker.conf`, is replaced, so make
config changes in `overrides.cfg` (see
[Changing settings](config-reference.md#changing-settings)). A copy of the
previous configuration is left in `printer_data/config/config_backups/`.

## Replace the printer configuration

```sh
bootstrap --replace configs
```

This moves your configuration to `printer_data/config/config_backups/` and
installs a fresh copy of your printer's configuration. The probe mode is picked
again, as on a [new install](calibration.md#probe-modes).

## K2 Pro installed with an older release

Older releases installed the K2 Plus configuration on a K2 Pro. After updating
Klipper, run:

```sh
bootstrap --update bootstrap configs
```

This installs the K2 Pro configuration. Your `overrides.cfg` and `SAVE_CONFIG`
block are not carried over; they stay in `printer_data/config/config_backups/`.
Copy back only what fits a K2 Pro, then calibrate again: bed mesh, Z offset
and PID (`BEDPID`, `NOZZLE_PID`). The probe mode is picked again, as on a
[new install](calibration.md#probe-modes).

After the [kernel and system update](#update-the-firmware-stack), the printer's
hostname changes from `K2Plus-XXXX` to `K2Pro-XXXX`. Update any saved address
that uses the old name.

## Add the nozzle camera

Printers installed with an older release show only the front camera. After the
[kernel and system update](#update-the-firmware-stack), run:

```sh
bootstrap --add-webcam
```

This adds the Creality nozzle camera to Fluidd and Mainsail and resets the
front camera's settings.

## Replace a component

```sh
bootstrap --replace klipper
bootstrap --replace moonraker
bootstrap --replace klippy-env
bootstrap --replace moonraker-env
bootstrap --replace fluidd
bootstrap --replace mainsail
bootstrap --replace helixscreen
```

Each command replaces only that component. `bootstrap --replace` on its own
replaces everything except HelixScreen.

## Other bootstrap commands

| Command | Behavior |
| --- | --- |
| `bootstrap` | First-time installation after flashing. |
| `bootstrap --probe` | Show the current probe mode. |
| `bootstrap --probe <mode>` | Change probe mode to `carto`, `mix` or `prtouch`. See [Calibration](calibration.md#probe-modes). |
| `bootstrap --set-timezone` | Detect the timezone from the public IP and apply it. |
| `bootstrap --add-webcam` | Add the front camera and the Creality nozzle camera to Fluidd and Mainsail. |

## Switch between custom and stock firmware

```sh
swap
```

This switches to the other firmware and reboots. Run it again to switch back.

Show which firmware is active without switching:

```sh
swap status
```

## Update Creality firmware

Creality firmware updates overwrite this firmware but keep your files.

1. Run `swap`. The printer reboots into Creality firmware.
2. Update Creality firmware the usual way.
3. SSH into the printer and run the
   [install command](install.md). When asked, choose
   **1 Reinstall, keep my setup**.
4. Power cycle the printer when instructed.

Your setup comes back after the power cycle. If it was working before the
update, you do not need to run `bootstrap` again.

After a Creality update, `swap` reports that this firmware was overwritten.
That is expected; run the installer.

!!! warning
    The factory reset on Creality firmware deletes everything stored for this
    firmware.

## Start over

Use the smallest reset that fixes the problem:

| Problem | Fix |
| --- | --- |
| Broken printer configuration | `bootstrap --replace configs` |
| Broken Klipper, Moonraker or a Python environment | `bootstrap --replace` |
| System files edited or deleted over SSH | [Reset system files](#reset-system-files) |
| Everything should be exactly as it was after the first install | [Fresh install](#fresh-install) |

### Reset system files

This deletes every change made outside `/mnt/UDISK`, such as edited or
deleted system files and programs installed with the system Python. Kalico,
Moonraker, Fluidd, Mainsail, your printer configuration and your print files
stay.

1. Run `swap`. The printer reboots into Creality firmware.
2. SSH into the printer and run the [install command](install.md). When
   asked, choose **2 Reinstall, reset system files** and answer `y`.
3. Power cycle the printer when instructed.

You do not need to run `bootstrap` again. The root password goes back to
`creality_2024`, Wi-Fi is copied from Creality firmware again, and you need to
run `bootstrap --set-timezone` again.

### Fresh install

A fresh install erases everything from this firmware and backs nothing up.
Download anything you want to keep from Fluidd or Mainsail first. Creality
firmware and its files are not touched.

1. Run `swap`. The printer reboots into Creality firmware.
2. SSH into the printer and run the [install command](install.md). When
   asked, choose **3 Fresh install** and type `erase` to confirm.
3. Continue from [First boot](install.md#first-boot).

To skip the menu, add `--fresh` to the end of the install command:

```sh
python3 -c "import urllib.request; exec(urllib.request.urlopen('https://github.com/Jacob10383/k2-plus-custom-firmware/releases/latest/download/install.py').read(), {'__name__':'__main__'})" --fresh
```
