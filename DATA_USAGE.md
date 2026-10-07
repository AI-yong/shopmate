# Data usage and redistribution

This repository contains code, database schemas, tests, and aggregate experiment
results. It does not redistribute the Amazon Reviews 2023 source metadata or
product images.

The local data pipeline was evaluated with a research subset derived from the
Amazon Reviews 2023 dataset. The dataset maintainers state that the dataset was
made available primarily for research and do not assign a license on behalf of
the underlying rights holders. Product images may also be owned by Amazon,
sellers, manufacturers, or other content suppliers.

For that reason:

- source metadata, downloaded images, manifests, database dumps, and object
  storage contents are excluded from Git;
- the local subset is intended only for non-commercial research and internal
  evaluation;
- users must obtain source data independently and review the applicable dataset,
  platform, copyright, privacy, and organizational requirements;
- this repository does not grant rights to use or redistribute third-party data;
- production or commercial use requires an approved, properly licensed catalog.

The processing, storage, embedding, retrieval, and evaluation code is designed
so that the local research subset can be replaced with an approved catalog
without changing the overall architecture.
