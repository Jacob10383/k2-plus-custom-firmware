# Updates & Recovery

## Update the firmware stack

```sh
bootstrap --update
```

This updates bootstrap and the firmware's Klipper extras. To update one part
only:

```sh
bootstrap --update bootstrap
bootstrap --update extras
```

## Update the printer configuration

```sh
bootstrap --update configs
```

This installs the latest [K2 configuration](https://github.com/Jacob10383/kalico/tree/main/config/k2)
and keeps your `SAVE_CONFIG` block, `overrides.cfg`, files you added and your
probe mode. A copy of the previous configuration is left in
`printer_data/config/config_backups/`.

## Replace the printer configuration

```sh
bootstrap --replace configs
```

This moves your configuration to `printer_data/config/config_backups/` and
installs a fresh copy of the
[K2 configuration](https://github.com/Jacob10383/kalico/tree/main/config/k2).

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
| `bootstrap --add-webcam` | Register the front webcam with Moonraker. |

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

## Start over with a fresh install

Use the smallest reset that fixes the problem:

| Problem | Fix |
| --- | --- |
| Broken printer configuration | `bootstrap --replace configs` |
| Broken Klipper, Moonraker or a Python environment | `bootstrap --replace` |
| Everything should be exactly as it was after the first install | Fresh install, below |

A fresh install erases everything from this firmware and backs nothing up.
Download anything you want to keep from Fluidd or Mainsail first. Creality
firmware and its files are not touched.

1. Run `swap`. The printer reboots into Creality firmware.
2. SSH into the printer and run the [install command](install.md). When
   asked, choose **2 Fresh install** and type `erase` to confirm.
3. Continue from [First boot](install.md#first-boot).

To skip the menu, add `--fresh` to the end of the install command:

```sh
python3 -c "import urllib.request; exec(urllib.request.urlopen('https://firmware.jacobean.xyz/install.py').read(), {'__name__':'__main__'})" --fresh
```
