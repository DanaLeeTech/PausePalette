# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-17

First release: plan a colour order for prints with more colours than AMS slots,
then write the manual swaps into the sliced G-code.

### Added

- Browser app running the Python core in Pyodide: jobs in IndexedDB, an offline
  service worker, installable, nothing uploaded.
- Command line: `plan`, `apply`, `convert`, `analyze`.
- Bambu Studio and OrcaSlicer dialects, multi-plate projects, unsliced projects.
- Reordered project output with the filament list already in swap order.
- Pauses checked against the plan, then verified byte for byte, with a swap
  checklist.
- Docker image, compose file and the GitHub Pages build.

### Changed

- The static PWA replaced the FastAPI server.
- The first layer's colour order can be pinned, position by position.
- Slicer pauses already in a file are kept and reported.

### Fixed

- Parsing of real Bambu G-code blocks.
- A `0` filament reference means "default", not filament 0.
