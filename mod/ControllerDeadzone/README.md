# Controller Deadzone Control 1.7.2-beta.3

Customize each controller stick's axial (X/Y) or circular (radial) deadzones in Borderlands 2.

**Contents:** [Installation](#installation) · [Setup and defaults](#setup-and-defaults) · [Native backend](#native-backend) · [TPS crash recovery](#tps-crash-recovery) · [Troubleshooting](#troubleshooting)

## Installation

1. Install PythonSDK and Willow2 Mod Manager; exit the game before updating.
2. Back up `sdk_mods/ControllerDeadzone/` and your saved `ControllerDeadzone.json`.
3. Extract the matching **Player ZIP** to `Borderlands 2/sdk_mods/`, replacing the old mod folder and removing duplicates. Do not replace shared SDK DLLs.
4. Confirm `sdk_mods/ControllerDeadzone/__init__.py`, then launch the game and enable the mod.

Saved settings are preserved on upgrade. To **roll back**, exit the game and restore your folder/settings backup. To **uninstall**, disable the mod, allow any pending restoration warning to resolve if possible, exit, and remove the folder.

## Setup and defaults

1. Select **Enable Axial Deadzone** or **Enable Radial Deadzone**. Only one mode can be active.
2. Open the matching **Axial Settings** or **Radial Settings** page; adjust the **Left Stick** and **Right Stick** independently.
3. Find input-switching and backend options under **General → Advanced Settings**.

| Option | Default |
| --- | --- |
| Axial mode | Off |
| Axial X/Y thresholds, each stick | 10% / 10%; both sticks enabled |
| Radial mode | On |
| Radial inner / outer, each stick | 10% / 100%; both sticks enabled |
| Keyboard & Mouse Priority | On |
| Controller Wake | 5% |
| Native Input Backend (BL2 or TPS) | Off |

**Axial** adjusts horizontal and vertical thresholds separately. **Radial** ignores motion inside the inner circle and reaches full output at the outer limit. Outer must exceed inner unless both equal 100%; 100% inner blocks the stick. Mixed keyboard/mouse protection remains partly active even when Priority is Off.

**Reset to Defaults** is under **General**.

## Native backend

The optional C++ backend is Off by default. Ordinary radial processing uses Python, while axial filtering uses the game's native behavior. Recoverable backend errors can fall back to Python, **but a native access violation can crash the game before fallback**.

TPS exposes a separate **Native Input Backend (Experimental)** option. Physical-controller and native TPS behavior have not been verified. Leave it Off unless you accept the risk of input problems or a crash.

Input fields changed by the mod are restored when possible on disable or input rebuild. If restoration cannot be verified, a warning may remain; restart the game if input stays affected.

## TPS crash recovery

1. Close TPS completely and back up its `ControllerDeadzone.json` settings file.
2. Inside `"options"`, change `"tps_experimental_native_radial"` to `false`, or restore the settings backup from before enabling the backend.
3. Save valid JSON, restart TPS, and leave the experimental backend Off.

## Troubleshooting

- **Missing mod:** Verify `sdk_mods/ControllerDeadzone/__init__.py` and remove duplicate installs.
- **Unexpected stick response:** Check the active mode and stick settings; compare with the mod disabled, then try the Python backend.
- **Settings missing:** Restore `ControllerDeadzone.json` from backup.
- **Input remains changed after disabling:** Let cleanup finish if possible, then restart the game.

**Compatibility:** Windows BL2 is the primary target. Co-op input isolation, controller reconnect edge cases, Proton/Wine, and TPS gameplay remain unverified; native Linux BL2 is unsupported by this Windows package.

[Compatibility and outstanding tests](https://github.com/PeytonBlakeslee/ControllerDeadzone/blob/main/docs/COMPATIBILITY.md) · [Report an issue](https://github.com/PeytonBlakeslee/ControllerDeadzone/issues) with versions, controller model, settings, reproduction steps, and a short redacted SDK log.
