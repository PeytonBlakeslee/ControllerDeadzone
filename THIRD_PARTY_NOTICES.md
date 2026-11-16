# Third-party notices and attribution

The original Controller Deadzone Control source is offered under the repository's [MIT license](LICENSE). This license does **not** relicense upstream libraries, bundled third-party code, or Borderlands 2.

## Runtime and build dependencies

- **UnrealSDK** by the bl-sdk contributors, LGPL-3.0: https://github.com/bl-sdk/unrealsdk
- **pyunrealsdk** by the bl-sdk contributors, LGPL-3.0: https://github.com/bl-sdk/pyunrealsdk
- **mods_base / Willow2 Mod Manager** by the bl-sdk contributors, LGPL-3.0: https://github.com/bl-sdk/mods_base and https://github.com/bl-sdk/willow2-mod-manager
- **pybind11** by its contributors, BSD-3-Clause: https://github.com/pybind/pybind11

Pinned SDK revisions are recorded in `mod/ControllerDeadzone/LICENSE-NOTICE.txt`
in the source repository. The same notice is installed as
`ControllerDeadzone/LICENSE-NOTICE.txt`. A native build generates
`dist/BUILD_INFO.json`, and final Player ZIPs are accompanied by
`.manifest.json` and `.sha256` files outside the installed mod folder.
These build artifacts are created during packaging, not tracked in source.

Upstream license texts are provided under [licenses/](licenses/). The source
repository does not redistribute game or SDK runtime DLLs. Preserve upstream
notices and comply with applicable requirements for native binaries.

## Other mod authors

No third-party mod source or specific other-mod research contribution is identified in the reviewed Controller Deadzone Control source for separate attribution. This is not a claim of a comprehensive legal provenance audit.

## AI-assisted development

AI tools assisted development and documentation. Peyton Blakeslee is the human project maintainer. AI tools are not listed as copyright holders.

Borderlands 2 and related trademarks and game assets belong to their respective owners. This is an unofficial, unaffiliated community mod.
