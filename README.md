# PU2LE — code excerpt

PU2LE is a distributed render-farm coordinator for Blender: it lets a studio turn its
own PCs — the machines artists already sit at — into a render farm, without renting
cloud GPUs. A user sends a project from a Blender add-on; the job gets split into
tiles, distributed across whichever PCs are online at the time (anywhere, no network
configuration required), rendered in parallel, and recomposed into the final image or
video automatically.

I'm the sole developer. It's a live product with real external users as of October
2026 (not open source, not a side project left unfinished).

**This repository is a curated excerpt, not the full source.** PU2LE is a commercial
product (see `LICENSE`), so the full backend, billing logic, and pricing details
aren't published here. What *is* included is real, unmodified production code — the
part that best shows how the system actually works: the distributed task scheduler.

## What's in this repo

- **`scheduler.py`** — the core of the whole system: decides which render tile each
  worker PC gets next, re-assigns work from slow/crashed workers, runs speculative
  re-execution for stragglers, and routes heavier tiles to GPU-capable machines. See
  the file's own header comment for the full context (what it depends on, why it's
  structured this way). Start here to understand how the distribution actually works.
- **`ricomponi.py`** — stitches the rendered tiles back into the final image.
- **`cloud_storage.py`**, **`config_griglia.py`** — small supporting modules
  `scheduler.py`/`ricomponi.py` reference, included so the logic reads cleanly in
  context.
- **`tests/`** — 2 of the 125 automated tests in the real suite (pytest + FastAPI's
  `TestClient` against a disposable SQLite DB, never production), covering the
  scheduler's reassignment/timeout logic and a GPU/CPU render-backend fallback system.
  They won't run standalone here (they import fixtures and app internals not included
  in this excerpt) — included for reading, not execution.

## Architecture, in short

1. A Blender add-on uploads the `.blend` file and splits the frame into a fixed tile
   grid (5×5 for images; an independently-sized chunk count for animations).
2. Worker executables (Windows/macOS/Linux, built via PyInstaller) poll the backend
   over plain HTTPS for work — purely outbound requests, so a worker can join from any
   network, behind any firewall/NAT, with zero configuration.
3. The scheduler (`scheduler.py`) hands out tiles, tracks liveness via periodic
   heartbeats, reassigns work if a worker goes silent, and speculatively re-executes
   tiles that are taking far longer than their peers — the same idea as MapReduce
   straggler mitigation, applied to a small, heterogeneous pool of consumer machines
   instead of a managed datacenter.
4. Once all tiles are in, the backend recomposes them (`ricomponi.py`) and uploads the
   result to object storage; the user's local dashboard picks it up automatically.

## Stack (for context)

FastAPI + PostgreSQL/SQLAlchemy backend, a Streamlit desktop dashboard, a Blender
add-on client, Cloudflare R2 for file storage, Stripe for billing, cross-platform
packaged executables via GitHub Actions + PyInstaller (with Apple notarization for
macOS). ~13,000 lines of Python across the full product, 125 automated tests.

## Why only this much

PU2LE has real users and a license that reserves all rights on the full product (see
`LICENSE`), so the whole backend isn't published here. This excerpt exists to give a
genuine look at how the system is actually built — real code, not a toy example —
without exposing the full commercial product.
