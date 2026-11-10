# DDE release regression review

This repository contains isolated test fixtures in peeweep-test. Green **review workflow** runs mean that the reproduction assertions passed, not that the tested defect is fixed.

The production source files in `sources/` are frozen and verified by SHA-256 before each test. Exact source commits are in `provenance.json`.

1. Two real Actions runs use the original controller concurrency expression. They call the original `record`, `member_state`, `check`, and `status` code against real GitHub PRs and branches. Only the owner prefix is redirected from linuxdeepin to peeweep-test. A barrier holds an old member-state snapshot while the other run merges a fixture PR and publishes a fresh result.
2. The original member guard first verifies a real ordinary PR. In the fault case, its final PR-list refresh is redirected to a local empty-body HTTP 503 endpoint. The actual gh binary handles that response; all other requests, including the final commit status, use the real GitHub API. This is fault injection, not a spontaneous GitHub outage.
3. The original action-sync Go program pushes the same template through the same temporary SSH deploy key to an unprotected control branch and to a branch requiring PRs and status checks, with administrator bypass disabled.

Only fixture branches are merged. No production repository, existing test release, or existing secret is changed. The temporary SSH key and secret are revoked after the experiments.
