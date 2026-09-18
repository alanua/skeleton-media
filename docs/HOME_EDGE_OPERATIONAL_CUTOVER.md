# Home Edge operational cutover

Skeleton Media is the canonical media implementation.

The live Home Edge composition app remains private because it also contains
non-media Home integrations. Operational separation therefore uses this
boundary:

- private Home Edge app.py: composition/API shell only;
- skeleton_media.cast: canonical player/resolver/discovery/IPTV/media code;
- ops/skeleton_cast/runtime: public UI/assets plus thin compatibility shims;
- alanua/Skeleton: approvals, registered executor, audit, rollback and cutover.

The production cutover must install one exact Skeleton Media git SHA into a
versioned release directory, install its Python package, refresh only the
public media runtime files while preserving the private composition app, then
restart and independently verify the real media state.

Direct deploy from this repository remains disabled.
