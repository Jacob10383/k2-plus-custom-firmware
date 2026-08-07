# Updates & Recovery

## Update the firmware stack

```sh
bootstrap --update
```

This updates bootstrap first, then installs changed firmware-managed Klipper
extras. Klipper restarts only when those files change.

To update one part only:

```sh
bootstrap --update bootstrap
bootstrap --update extras
```

## Replace the printer configuration

```sh
bootstrap --replace configs
```

This moves the current configuration into
`printer_data/config/config_backups/<timestamp>/`, installs a fresh K2
configuration, and restarts the core services.

!!! warning
    This replaces your active configuration. Use it for recovery or when you
    intentionally want a fresh firmware configuration.

## Replace a component

```sh
bootstrap --replace klipper
bootstrap --replace moonraker
bootstrap --replace fluidd
bootstrap --replace mainsail
bootstrap --replace helixscreen
```

Available replacement targets are `klipper`, `moonraker`, `klippy-env`
(`klipper-env` is accepted as an alias), `moonraker-env`, `fluidd`, `mainsail`,
`configs`, and `helixscreen`.

Targets are literal, not dependency resolution. Replace individual components
only when you know the paired environment or repository is already present.
Bare `bootstrap --replace` rebuilds the core managed stack but does not include
HelixScreen.

## Other bootstrap commands

| Command | Behavior |
| --- | --- |
| `bootstrap` | First-time installation after flashing. |
| `bootstrap --probe` | Show the current probe mode. |
| `bootstrap --probe carto\|mix\|prtouch` | Change probe mode and restart Klipper. See [Calibration](calibration.md#probe-modes). |
| `bootstrap --set-timezone` | Detect the timezone from the public IP and apply it. |
| `bootstrap --add-webcam` | Register the front webcam with Moonraker. |

## Switch between custom and stock firmware

```sh
swap
```

The printer prepares the other environment and reboots. Both environments are
preserved, so running `swap` again returns to the previous one.

Show the current environment and available slots without switching:

```sh
swap status
```
