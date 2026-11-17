# Controller Deadzone Control: Release History

This is the concise player-facing changelog. Detailed historical development and validation records remain in the private development archive and are not included in this clean production history. The approved source, Release notes, and original manifest provide public release provenance.

## 1.7.2-beta.3 (release candidate)

- Reconciled the accepted player source and corresponding optional native backend source on the private release-preparation branch.
- Preserved per-stick axial/radial behavior, mixed keyboard/controller protections, cleanup, fallback and existing saved option identities.
- Clarified labels for axial/radial selection, controller wake, mixed input and the experimental backend.
- Corrected the displayed name to **Controller Deadzone Control**. No gameplay or native API change is claimed for this UI correction.
- Experimental TPS support remains **unverified** with physical controllers; opting into TPS native processing may crash.

## Earlier 1.7.x / 1.6.x / 1.4.x work

The historical development log documents controller identification, per-stick filters, radial math, engine-field ownership/restoration, recovery after input rebuilds, native backend verification and backwards-compatible settings. These historical changes are not being relabeled as newly verified releases.

The Player ZIP version, original build commit, native binary hash, and recorded acceptance status are documented by the Release notes and accompanying `.manifest.json` and `.sha256` files.
