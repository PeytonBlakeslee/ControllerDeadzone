# Controller Deadzone Control: Compatibility

Version **1.8**. Limited Windows BL2 gameplay observations and source checks do not establish full multiplayer or cross-platform support.

**Contents:** [Platform status](#platform-status) · [TPS native risk](#tps-native-risk) · [Uncompleted test checklist](#uncompleted-test-checklist) · [Reporting issues](#reporting-issues)

## Platform status

| Environment | Status |
| --- | --- |
| Windows Borderlands 2 | Primary Player target; final clean-install/controller acceptance pending |
| The Pre-Sequel | Experimental; physical-controller gameplay untested |
| Co-op host/client | Local input intended; two-system isolation unverified |
| Windows BL2 via Proton/Wine | Unverified, including native backend |
| Native Linux BL2 | Unsupported by this Windows package |

See the [player guide](../mod/ControllerDeadzone/README.md) for options and recovery.

## TPS native risk

TPS defaults to Python and offers a separate, **Off-by-default** experimental C++ backend. Enabling it may cause input problems or crash the game before Python can recover. This risk has not been cleared by TPS gameplay testing. [Recover after a crash](../mod/ControllerDeadzone/README.md#tps-crash-recovery).

## Uncompleted test checklist

These checks are **proposed, not passing results**:

1. Join a separate co-op host and verify that left/right axial and radial adjustments affect only the local controller.
2. Switch keyboard/mouse to controller, travel, and reconnect the device. Check input and owned-field restoration.
3. Test Windows BL2 under Proton/Wine, distinguishing Python behavior from a matching Win32 native extension.
4. Test TPS with a disposable setup and settings backup, recording physical-controller and optional native behavior.

## Reporting issues

[Open an issue](https://github.com/PeytonBlakeslee/ControllerDeadzone/issues) with game/SDK/mod versions, controller model, relevant options, OS and solo/host/client role, reproduction steps, and redacted log excerpts.
