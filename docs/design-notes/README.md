# Design notes

Point-in-time design documents, preserved deliberately. Unlike the planning
scratch that #190 untracked (published by accident, half of it never pushed),
each file here is a complete record of a design that shaped code now on
`main` — or, in one case, of an experiment that was abandoned. They were
recovered from the retina-test droplet in August 2026, when it was the only
machine still holding them.

These are historical documents, not living ones. Where a note and the code
disagree, the code wins; nothing here is updated when the code moves. Paths,
commands and line numbers inside them describe the tree as it stood on the
date in the filename.

| Document | Status |
| --- | --- |
| [2026-07-28-docker-compose-consolidation.md](2026-07-28-docker-compose-consolidation.md) ([design](2026-07-28-docker-compose-consolidation-design.md)) | Shipped. The plan behind collapsing the per-environment compose files and hand-written nginx configs into one base + thin overlays rendering a shared template (landed via #144; #140/#143 were earlier attempts). Explains *why* the compose files have the shape they do. |
| [2026-08-05-solver-process-boundary.md](2026-08-05-solver-process-boundary.md) | Partially shipped. Designs moving the CPU-bound multinode solve behind a picklable pure-function seam into a `ProcessPoolExecutor`. The `SOLVER_POOL`-gated process pool landed via #201; the full boundary described here (plain-dict arguments, parent-side state mutation audit) remains the reference for finishing the job. |
| [2026-08-06-node-api-v1.md](2026-08-06-node-api-v1.md) | Shipped. The task-by-task plan behind the v1 node API (#152, #164, #166–#189): tables, bearer tokens, wire models, router, rate limits, config versioning, Mender lookup. |
| [2026-08-06-node-ingest-minimal.md](2026-08-06-node-ingest-minimal.md) | Shipped in reduced form. The minimal-ingest cut of the node API plan, plus the Cloudflare origin-boundary steps that landed via #150. |
| [2026-05-20-spectrum-analyser-experiment.patch](2026-05-20-spectrum-analyser-experiment.patch) | Abandoned experiment, kept as a raw patch. An RF-profile endpoint and spectral-measurement handling for tower ranking (May 2026, never PR'd). Its branch was deleted in the August 2026 pruning; this file is now the only copy. Apply with `git apply` onto a commit near its date if it's ever revisited. |
