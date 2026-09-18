# Skeleton Cast media runtime

This directory is the repository-owned source for the media service extracted from Skeleton.

## Status

The reusable media modules, UI assets, resolver/player/discovery stack, IPTV support, media state, Trakt sync and current public-safe Android client source are now tracked here.

The production Home Edge service is not cut over yet. Its current app controller also contains unrelated private Home integrations. For that reason deploy.sh and rollback.sh are deliberately fail-closed in this extraction.

## Production rule

A future cutover must:
1. split or adapt the remaining production route glue;
2. build/test an exact source revision;
3. create a verified rollback backup;
4. execute only through the registered Skeleton Home Edge executor;
5. independently verify the service, foreground/player state and physical media output;
6. persist audit evidence and canonical source provenance.

No SSH-side direct deployment is supported by this repository.
