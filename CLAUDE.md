# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

GPUStack is an open-source GPU cluster manager for AI model serving and GPU instance provisioning. A single **server** orchestrates many **workers** (across on-prem, Kubernetes, and cloud clusters), schedules models onto GPUs, configures inference engines (vLLM, SGLang, TensorRT-LLM, Ascend MindIE, vox-box), and exposes OpenAI-compatible APIs behind a built-in gateway. The whole product ships as one Python package (`gpustack`); the same binary runs as either server or worker depending on flags.

> Fork / secondary-development design docs: [`docs/secondary-development-pipeline.md`](docs/secondary-development-pipeline.md) (custom build & image pipeline, repo relationships) and [`docs/lightx2v-backend-design.md`](docs/lightx2v-backend-design.md) (wrapping the LightX2V video engine as a backend, elastic 1/2/4-GPU). Read these before touching packaging or adding a new inference engine.

## Common commands

The Makefile dispatches to scripts in `hack/`. All Python runs through `uv`.

| Command | What it does |
|---|---|
| `make install` | Install dev tooling (uv, pre-commit hooks, deps). Run once after clone. |
| `make deps` | `uv sync --all-packages && uv lock && uv tree`. Run after changing dependencies. |
| `make lint` | Runs `pre-commit run --all-files` (flake8 + black + shellcheck). |
| `make test` | Runs `uv run pytest`. |
| `make generate` | Regenerates API clients (see "Generated code" below). |
| `make build` | `uv build` → artifacts in `dist/`. |
| `make package` | Builds container images (Docker required; not on Windows). |
| `make ci` | install → deps → lint → test → build. |

Run a single test: `uv run pytest tests/scheduler/test_scheduler.py::test_name -v`
Run a directory: `uv run pytest tests/policies/`

Add dependencies with `uv add <pkg>` (or `uv add --dev <pkg>`) — never hand-edit `pyproject.toml` deps, since `uv.lock` must stay in sync.

## Running locally for development

The server requires a real database (PostgreSQL or MySQL — not SQLite for dev). Start Postgres, then:

```bash
uv run gpustack start \
  --database-url postgresql://postgres:mysecretpassword@localhost:5432/postgres \
  --gateway-mode disabled \
  --api-port 80
```

`--gateway-mode disabled` skips the embedded Higress gateway, which is what you want when developing the API itself.

The CLI entrypoint is `gpustack.main:main`; subcommands are wired up in `gpustack/cmd/` (`start`, `reload-config`, `download-tools`, `migrate`, image management, `reset-admin-password`, `version`, `prerun`). `gpustack start` runs the server, the worker, or both depending on flags defined in `gpustack/cmd/start.py`.

## Packaging & related repos

Two artifacts are produced: a **Python wheel** and a **container image**. Both pull assets from sibling repos at build time — this repo (`gpustack`) is the backend + orchestration core only.

**`make build`** (`hack/build.sh`) → wheel in `dist/`. Sequence: `prepare_dependencies` → `set_version` (writes git version into `gpustack/__init__.py` + `pyproject.toml`, restored afterward) → `uv build`. `[tool.hatch.build]` bundles `gpustack/ui` and `gpustack/third_party` into the wheel as artifacts, so the UI must be downloaded *before* building. Publish with `hack/publish-pypi.sh` (twine).

**`make package`** (`hack/package.sh`) → container image via `docker buildx` from `pack/Dockerfile`. Needs Docker, not on Windows. Key env vars: `PACKAGE_TAG` (default `dev`), `PACKAGE_ARCH` (`amd64`/`arm64`), `PACKAGE_PUSH` (`false`), `PACKAGE_UI_DOWNLOAD` (`true`), `PACKAGE_NAMESPACE`/`PACKAGE_REPOSITORY` (`gpustack`).

External assets pulled in by `hack/install.sh` (runs inside `make build` / `make install`):
- **gpustack-ui** — the frontend is a *separate repo, not built here*. `download_ui()` fetches a pre-built `dist` tarball from Tencent COS (`gpustack-ui-…cos…/releases/<tag>.tar.gz`) into `gpustack/ui/`. Version alignment: a real release tag (`vX.Y.Z`) downloads the matching UI, otherwise falls back to `latest`.
- **gpustack/community-inference-backends** — `make_community_backends()` git-clones it, runs `make`, and embeds the resulting `community-inference-backends.yaml` into `gpustack/assets/`.
- `copy_extra_static()` copies this repo's `static/` (catalog icons) into `gpustack/ui/static/`.

Base images `FROM`'d in `pack/Dockerfile` (runtime control-plane components): `gpustack/gpustack-operator`, **Higress** suite (`mirrored-higress-api-server`/`higress`/`pilot`/`gateway` — the embedded AI gateway used by `gpustack/gateway/`), Prometheus + Grafana (built-in observability), plus a bundled PostgreSQL.

Sibling PyPI packages (in `pyproject.toml` deps): `gpustack-runner` (inference engine runner), `gpustack-runtime` (runtime/accelerator detection), `gpustack-higress-plugins` (gateway plugins).

## Fork overlay image (`pack/Dockerfile.acr`)

Deployed clusters do **not** run `make package` output. They run an overlay image: upstream
`gpustack:v2.2.3` + this fork's changed files + the `gpustack-ui` `main` build, produced by the
"Pack GPUStack overlay to ACR" workflow.

- **Every `gpustack/**/*.py` you change must be added to the overlay's COPY list.** Anything not
  listed ships as the *base image's* version — silently, with a green build.
  `hack/check-overlay-list.sh` is the gate (CI runs it first; it fails on both missing and stale
  entries). Run it locally before pushing: `git fetch --no-tags https://github.com/gpustack/gpustack.git refs/tags/v2.2.3:refs/tags/v2.2.3 && bash hack/check-overlay-list.sh`.
- A new migration must be listed there too, and the Dockerfile's head assertion updated to the new
  `alembic head` (otherwise the running server's `alembic upgrade` goes the wrong way).
- `pack/Dockerfile.acr` keeps a `py_compile` fail-fast list — add new modules there as well.

## Fleet operations (upgrading a live cluster)

Runbook: [`docs/lightx2v-节点运维手册.md`](docs/lightx2v-节点运维手册.md) — §2.5 is the standard
copy-paste flow, §2.6 defines what actually counts as proof an upgrade landed. Operator scripts
live in [`docs/scripts/`](docs/scripts); on the manager they are `/root/lx2v-node.sh`,
`/root/lx2v-fleet.sh`, `/root/lx2v-rebuild-instances.py` (workers fetch `lx2v-node.sh` from
`/nfs-models/_transfer/` on each fleet run).

Order: **manager server → workers → engine images → instances**. Server must not be older than
workers (DB/API direction), and pulling an engine image never changes running instances — they pin
the image ID they were created with.

Rules learned the hard way (details in the runbook):

- **A green tick is not proof.** `lx2v-fleet.sh` once reported `OK=55 FAIL=0` while most nodes
  still ran the previous image: ACR rate-limited the pull, the node fell back to a stale NFS tar,
  and the script still exited 0. Check the per-node `LX2V_IMAGE_ID` / three-tier summary /
  `--verify-rev <commit>`; **exit 76 = unverified, not success**.
- **ACR rate-limits parallel pulls** (`-j 4` online already hit 21/55 `too many requests`). The fix
  is `lx2v-node.sh prepare-transfer` (refresh NFS tars) + `--offline`, not more retries.
- **Deleting an instance does not stop its container** after a worker image upgrade (the worker no
  longer adopts containers it did not start). The old container keeps the GPU and the replacement
  dies with CUDA OOM — use `lx2v-rebuild-instances.py`, which stops the container first, serialises
  per node and verifies the resulting image ID.
- **Instances stuck in `Starting`** are a worker-side state-sync failure (`sync_model_instances_state`
  in the worker log), not a broken instance — don't delete them in a loop.
- Delete any credential you mint for inspection (admin JWT from `jwt_secret_key`, temporary API
  keys) before you finish.

## Architecture

### Server / worker split
- **`gpustack/server/`** — the control plane. `server.py` boots everything; `app.py` builds the FastAPI app (middlewares, auth, lifespan HTTP clients). The core pattern is **controllers** (`server/controllers.py`): long-running async reconcilers (`ModelController`, `ModelInstanceController`, `WorkerController`, `ClusterController`, `ModelRouteController`, `GPUInstanceController`, etc.) that watch DB state and drive resources toward their desired spec, Kubernetes-operator style. Background collectors/archivers (metrics, usage, resource events, system load) also live here.
- **`gpustack/worker/`** — the data plane on each node. `worker.py` registers with the server; `serve_manager.py` launches/monitors inference processes; `backends/` has one module per inference engine (`vllm.py`, `sglang.py`, `ascend_mindie.py`, `vox_box.py`, `custom.py`, all extending `base.py`). Also handles model file downloads, tool/dependency management, and metrics collection.

### Scheduling (`gpustack/scheduler/` + `gpustack/policies/`)
The scheduler decides which worker/GPU(s) a model instance runs on. It's a **filter-then-score chain**:
- `policies/worker_filters/` narrow the candidate set (by label, GPU match, backend framework, cluster, status, local path).
- `policies/candidate_selectors/` do engine-specific resource-fit checks (`VLLMResourceFitSelector`, `SGLangResourceFitSelector`, `GGUFResourceFitSelector`, `AscendMindIEResourceFitSelector`, custom backend).
- `policies/scorers/` rank survivors (placement, offload-layer, model-file-locality, status).
`scheduler/calculator.py` and `evaluator.py` estimate resource requirements; `model_registry.py` / `meta_registry.py` detect model type and metadata.

### Data model & persistence
- **`gpustack/schemas/`** — SQLModel models that are simultaneously the DB tables and the API request/response shapes. This is the source of truth for the domain (models, model instances, model files, workers, clusters, GPU instances, users/orgs, routes, providers, benchmarks).
- **`gpustack/migrations/`** — Alembic. Generate a revision against a running DB with `hack/generate-migration-revision.sh "message"` (uses `DATABASE_URL`, autogenerate). Migrations are excluded from black/flake8.
- DB access is async (`server/db.py`, `async_session`); supports SQLite/Postgres/MySQL.

### Gateway & proxying
- **`gpustack/gateway/`** — manages an embedded Higress/Envoy AI gateway that fronts inference traffic (model routing, load balancing, metrics). `--gateway-mode` controls it.
- **`gpustack/http_proxy/`** and **`gpustack/websocket_proxy/`** — the server proxies inference requests through to workers; the websocket proxy (with a Patricia-trie router and message client/server) tunnels traffic to workers that may be behind NAT.

### API surface (`gpustack/routes/`)
One module per resource group, assembled in `routes/routes.py`. `openai.py` / `rerank.py` serve the OpenAI-compatible inference endpoints; the rest are management APIs. Auth scopes (`management_scope`, `inference_scope`, admin/cluster/worker principals) are defined in `gpustack/api/auth.py`.

### Cloud & GPU instances
- **`gpustack/cloud_providers/`** — pluggable provisioners (`abstract.py` base, `digital_ocean.py`); `gpustack/gpu_instances/` and `gpustack/k8s/` handle launching SSH-accessible GPU instances and rendering Kubernetes manifests (Jinja templates).

## Generated code — do not edit by hand

Files under `gpustack/client/` named `generated_*.py` (and the `ClientSet`) are produced by `gpustack/codegen/` (Jinja templates in `codegen/templates/`) from the schema classes listed in `codegen/generate.py`. After changing a schema that has a generated client, run `make generate`. Editing the generated files directly will be overwritten.

## Conventions

- **Formatting:** black, line-length 88, `skip-string-normalization` (don't normalize quotes). flake8 max-line-length 88 but E501 is ignored; complexity cap is 10 in CI config but pre-commit allows 15. `migrations/` and `*generated*` are excluded from linting.
- Always run `make lint` (or let pre-commit run) before committing — the hooks also run shellcheck on `hack/` scripts.
- Python 3.10–3.12 only.
- Tests are pytest with `pytest-asyncio`; the root `conftest.py` provides an autouse global `Config` + temp data dir fixture, so tests get a configured environment for free.
