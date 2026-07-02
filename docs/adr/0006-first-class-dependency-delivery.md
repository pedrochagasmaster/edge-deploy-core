# First-class dependency delivery

Dependency files and wheel bundles form one reviewed release input. When configured
`dependency_paths` change, the release engine builds a clean target-specific wheelhouse
from the reviewed GitHub source commit, writes a deterministic manifest, transfers the
archive over the authenticated SSH control connection, and verifies it before changing
the node checkout.

Bundle identity covers the reviewed source SHA, LF-normalized dependency-file hashes,
wheel hashes, and supported Python, ABI, and platform tags. Nodes stage bundles under
`/ads_storage/$USER/.edge-deploy/bundles/<tool>/<digest>`. Extraction is clean and
atomic; unexpected files, stale wheel versions, incompatible Python, insufficient disk,
or failed offline resolution stop the release before update.

Resume may reuse a stage only when its digest, source SHA, and dependency hashes match.
After installation succeeds, `current` points to the verified content-addressed stage.
Tools with no dependency configuration skip this phase.

The Tool installer remains responsible for its user environment and launcher. It must
consume `EDGE_DEPLOY_BUNDLE_DIR` and must not use an online package index during a
release.
