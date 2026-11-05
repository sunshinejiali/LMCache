# Cache-server ownership migration (#6311)

Status: implementation plan for discussion, not a completed migration.

Parent: LMCache/LMCache#6305. Scope: LMCache/LMCache#6311 in full, delivered
through multiple responsibility-scoped PRs. This branch is based on upstream
`dev` at `dc8c116decb899333e6f214bdc36361e4bcdb0ba`.

The governing contract is `docs/design/v1/package_boundaries.md` at PR #6244
revision `92a1ffc8`. That revision describes a skeleton and future ownership;
it is not evidence that the prerequisite implementations have moved. Neither
that PR nor the other prerequisite issues is silently merged into this branch.

## Outcome and exclusions

The canonical implementation of the cache-server service will live under
`lmcache.v1.server.cache_server`: storage construction and ownership, L1/L2
control loops, cache-server eviction execution, request business operations,
server module composition, application startup/shutdown, and its HTTP APIs.

The full `lmcache` wheel remains the complete product. Supported old imports,
CLI arguments, plugin configuration, HTTP contracts, storage semantics, operation
IDs and wire representations remain compatible. Legacy modules become explicit
compatibility facades only after their relevant symbols have migrated.

This issue does not own inference-worker lifecycle, shared DTO/protocol
extraction, generic inbound transport infrastructure, other server products,
client-wheel construction, or release. No cache policy, kernel, transport
protocol, authentication behavior or performance tuning is redesigned here.
Migration implementation may consume those owners' new interfaces without
absorbing their responsibilities.

## Target organization

Names below are proposed service-local organization, not new public API promises.

```text
lmcache/v1/
  common/                         #6307: shared DTOs and genuinely pure helpers
  transport/                      #6309: wire contracts, schemas and codecs
  client/                         #6310: worker-side workflows
    transport/                    #6308: outbound transport
  platform/                       device primitives; no cache-server imports
  server/
    transport/                    #6308: reusable inbound execution/dispatch
    cache_server/
      config.py                   cache-server-owned configuration and parsers
      server.py                   MPCacheServer, composition and process lifecycle
      context.py                  MPCacheServerContext and live server registry
      session.py                  server-side request/session bookkeeping
      modules/
        base.py                   module construction/discovery contract
        external.py               cache-server plugin composition
        management.py             server administration and liveness authority
        lookup.py                 lookup business execution
        transfers/                server-side store/retrieve orchestration
        blend/                    server-side blend request execution
        p2p.py                    cache-server peer-cache operations
      storage/
        manager.py                StorageManager and write/prefetch orchestration
        config.py                 storage-owned construction configuration
        l1/                       L1 object/lock/pool ownership
        l2_adapters/              adapter implementations and one factory registry
        controllers/              store, prefetch and eviction control loops
        eviction/                 cache-server bindings and execution policy wiring
        serde/                    server storage serialization implementations
      http/
        app.py                    cache-server FastAPI app/lifespan
        registry.py               cache-server route discovery
        dependencies.py           service context and dependency wiring
        routes/                   cache/config/quota/reconfiguration/etc.
  server/frontend/                #6312: fleet frontend, a separate service
  server/mp_coordinator/          #6313: fleet coordinator, a separate service
  server/memory_orchestrator/     #6314: allocation authority, a separate service
```

Service code may depend on common/transport/platform and approved outbound
client transport. Common and shared transport must not import service code.
Client, SDK and integrations must not reach cache-server implementation through
initializers, lazy imports, `TYPE_CHECKING`, platform helpers or compatibility
facades. Server services must not import inference-engine integrations.

## Inventory and responsibility boundaries

The inventory is based on actual symbols and callers at the recorded dev SHA.
Re-audit it when rebasing; a filename or directory is not an ownership decision.

| Current surface | Cache-server responsibility | Keep elsewhere / split first |
| --- | --- | --- |
| `multiprocess/server.py` | `MPCacheServer`, `ServerBuildComponents`, module composition, `run_cache_server`, process startup/shutdown | Inbound listener construction consumes #6308; shared config/DTOs follow their owners |
| `multiprocess/engine_context.py` | `MPCacheServerContext`, live `LayoutDescRegistry`, lookup read-lock release | `ShmPoolInfo` and registration response shapes need shared-contract review; layout/key/replication descriptions belong to #6307/#6309 |
| `multiprocess/session.py` | Server-side `Session` / `SessionManager` state and cleanup | Worker subscription/registration/transfer workflow remains #6310-owned |
| `multiprocess/engine_module.py` | `EngineModule`, `DiscoverableModule`, `ModuleBuildContext`, service module ordering/discovery, server-side liveness targets | Reusable handler scheduling/dispatch belongs to #6308; wire op definitions belong to #6309 |
| `multiprocess/ext_server_module.py` | External server-module specification/composition and module loading | Request/response wire DTOs belong to #6309; generic ZMQ/gRPC registrar machinery belongs to #6308 |
| `multiprocess/modules/*` | Lookup, management, transfer, blend, KV-event and peer-cache business execution | Do not move worker transfer contexts or shared SHM/tensor-wire contracts with their server consumers |
| `multiprocess/cache_control/*` | Server object/prefetch services and execution | Split request DTOs/encoding/transport clients by symbol |
| `multiprocess/config.py` | `MPServerConfig`, server runtime-plugin config and their parsers; local HTTP binding configuration | Do not move the complete mixed module; coordinator/event DTOs and outbound configuration require ownership review |
| `multiprocess/client_config.py`, `transfer_context/*`, `heartbeat.py`, `futures.py`, `kv_event_subscriber.py` | Only demonstrably server-owned symbols, if any | Worker workflow/outbound execution stays with #6310/#6308; shared codecs/IPC primitives follow transport/platform |
| `multiprocess/request_handler.py`, `affinity_pool.py`, `transport/*`, `rpc.py`, `protocols/*` | Cache service consumes their contracts | Generic inbound dispatch/executors: #6308; outbound calls: client transport; shared wire: #6309 |
| `multiprocess/http_server.py`, `http_api_registry.py`, `http_apis/*` | The cache server's own HTTP app, routes and service dependencies | This is not `lmcache/lmcache_frontend`; shared HTTP DTOs belong to common/transport; reusable auth/discovery helpers need caller review |
| `distributed/storage_manager.py` | `StorageManager`, `l1_owners_of`, lifecycle and server orchestration | Shared prefetch/key/layout descriptions are not owned here |
| `distributed/l1_manager/*`, `memory_manager/*` | L1 lock/object lifecycle, server pools and storage allocations | Device primitives/native kernels stay in platform/native support; do not move `MemoryObj` or shared descriptors merely because a pool uses them |
| `distributed/l2_adapters/*`, `serde/*`, `transfer_channel/*` | Cache storage adapters, their construction and storage serialization/peer transfer | Split generic schemas/codecs/protocols before moving an implementation; preserve out-of-tree extension imports and names |
| `distributed/storage_controllers/*` | Store/prefetch/eviction execution and adapter lifecycle | Review internal DTOs used across services/controllers instead of wholesale relocating `internal_api.py` |
| `distributed/api.py`, `internal_api.py`, `error.py`, `key_codec.py`, `bitmap_ops/*` | Only truly service-local implementation | Mixed shared contracts/helpers: #6307/#6309; no bulk move to cache_server |
| `distributed/eviction.py`, `eviction_policy/*`, `quota_manager.py` | Local cache-server eviction wiring and quota enforcement | Coordinator also consumes `EvictionPolicy`, `IsolatedLRUEvictionPolicy`, `QuotaManager`; resolve shared implementation ownership with #6313 before moving those symbols |

## Prerequisite decisions and merge gates

1. **Package skeleton (#6244).** Use the agreed namespace; do not independently
   redefine the package boundary or land conflicting marker implementations.
2. **Shared data (#6307).** Agree canonical imports/identity for `ObjectKey`,
   tags, layout/replication descriptions, prefetch results, listeners and lock/
   allocation descriptors actually used across endpoints. Pure descriptions
   are distinct from the mutable managers that own them.
3. **Wire and endpoint transport (#6309/#6308).** Agree handler/operation types,
   SHM/tensor framing, server request interfaces and transport registration.
   Generic inbound affinity/executor machinery stays in server.transport.
4. **Worker/platform closure (#6310).** Resolve the platform-to-service reverse
   dependencies below before moving transfer orchestration/GDS state.
5. **Other service consumers (#6313/#6314).** Agree ownership for shared stateful
   quota/eviction helpers and persistence contracts. Do not move stateful code
   to common merely because two server services consume it.

A storage-local slice can be prepared and tested independently where its
contracts are stable. It may temporarily consume a legacy shared contract, but
that edge must be documented with its owner and replaced before the final
boundary gate. A PR that changes a still-unsettled shared interface waits for
its prerequisite; it does not vendor a second definition or bypass the gate.

### Two concrete reverse dependencies to fix before transfer migration

- `platform/base/cache_context.py::transfer_object_group` currently imports
  `multiprocess.object_group_transfer`, which invokes storage staging and GDS.
- CUDA/MUSA cache contexts import buffer registration helpers from
  `gpu_connector.gds_context`, whose implementation consumes storage config.

Simply moving those helpers into cache_server would make platform/client
imports reach server implementation. With #6310, separate device layout-copy
primitives and buffer-registration capabilities from cache-server staging/slab
ownership. The service should supply its storage-specific operations through a
reviewed narrow binding/injection boundary; platform must not import the service
to find a global singleton. Preserve existing vendor context overrides/factories
and validate both source imports and runtime behavior. The exact extension
contract must be agreed before its dependent move, rather than introduced as a
side effect of renaming files.

The external GDS backend RFC remains a separate change. It must use the final
interface placement or compatibility re-exports, without forcing the client
wheel to include cache-server discovery or native storage dependencies.

## Proposed PR series

These are responsibility groups. Subrows are separate PRs when substantial;
they are not instructions to combine an entire phase into one large PR.
No individual partial migration closes #6311.

| Series | Responsibility / source scope | Canonical destination | Required evidence |
| --- | --- | --- | --- |
| S1 | Cache-server/storage configuration symbols; explicit old exports | `cache_server/config.py`, `storage/config.py` | CLI/config parsing and defaults; JSON equality; mixed coordinator/client symbols stay out |
| S2a | DRAM L1 object/lock/pool ownership and its factory binding | `storage/l1` | Reserve/read/write/free/TTL/rollback; owner IDs and shared-memory lifetime |
| S2b/S2c | Device-DAX and GDS L1 ownership, separately | `storage/l1` | Allocation/range/alignment, teardown and readiness; GDS requires the platform binding gate |
| S3a | L2 adapter interface/config/factory registration ownership | `storage/l2_adapters` | Old/new class identity; one registry; lazy loading; external plugin factories and config unchanged |
| S3b+ | Concrete L2/serde/storage-channel implementations, split into independently reviewable backend families | Service-local storage subpackages | Existing adapter behavior, package data, optional dependency isolation, plugin and native-module names |
| S4a | Store controller and write/store policy execution | `storage/controllers` | L1 read locks retained until L2 completion; errors, eventfds, byte accounting, close |
| S4b | Prefetch planning/execution and lock admission | `storage/controllers` | Grouped rows, shard-reader counts, hit bitmaps, one-shot results, cancellation/unlock |
| S4c | Local eviction execution and quota enforcement | `storage/eviction`, controller binding | LRU/ARC/isolation semantics, locked keys, quotas, persistence; shared coordinator helpers have agreed ownership |
| S5 | StorageManager composition, lifecycle and dynamic adapter/pool management | `storage/manager.py` | Existing storage manager/L2 reconfiguration tests; stable public methods and status shape |
| S6 | Server session/layout registry/context ownership | `session.py`, `context.py` | Session expiry/cleanup, shared registration references, key identity and replicated-KV reader counts |
| S7a | Engine-module construction/discovery lifecycle | `modules/base.py` | Deterministic ordering, dependency errors, configured overrides, exactly-once discovery and teardown |
| S7b | External server-module plugin composition | `modules/external.py` | Old plugin imports/decorators, CLI JSON, external implementations, ZMQ/gRPC registration through #6308 |
| S8a+ | Server request business execution, one family per PR: management; lookup; engine-driven transfer; LMCache-driven transfer; blend; P2P/KV-events | `modules`, service-local cache control | Matching existing behavioral suites; unchanged op IDs/wire types; no worker orchestration/codecs absorbed |
| S9 | MPCacheServer composition and process/CLI lifecycle | `server.py` | CLI flags, startup/shutdown ordering, module status, signal/error cleanup, old module execution paths |
| S10a | Cache-server HTTP app/lifespan and route registry | `http/app.py`, `http/registry.py` | Startup state, authentication, OpenAPI/route parity, plugin launch and reverse cleanup |
| S10b+ | Cache/config/info/quota/reconfiguration HTTP execution, split by API family | `http/routes` | Methods, URLs, request/response/error bodies, auth coverage and service behavior unchanged |

Storage and execution tracks can advance in parallel after their prerequisites.
S9 integrates the canonical pieces after S5-S8. HTTP route migration consumes
canonical services; it does not pull the fleet frontend or coordinator app into
the cache-server namespace. Keep small policy/lifecycle/DTO preparation in the
PR that owns it; split any row whose diff is not independently reviewable.

## Compatibility strategy

Each moved public class/function has one canonical definition. A legacy module
explicitly imports and exports the supported symbols, without wildcard exports,
wrapper subclasses, copies of implementation, or a process-wide import hook.

Example, after its prerequisites land:

```python
# lmcache/v1/distributed/storage_manager.py
from lmcache.v1.server.cache_server.storage.manager import (
    StorageManager as StorageManager,
    l1_owners_of as l1_owners_of,
)
```

Tests assert identity (`legacy.StorageManager is canonical.StorageManager`),
not only matching class names. Private test monkeypatches move to the actual
canonical implementation; compatibility guarantees must not silently promise
that assigning a facade's copied module globals changes another module's state.
Document public module-level mutable state that needs a stronger bridge.

Preserve these surfaces throughout the series:

- `lmcache server`, legacy module entry points and their command-line behavior;
- import strings for external server modules/L2 plugins and their config classes;
- HTTP route methods/paths, auth/error handling and body schemas;
- RPC operation IDs, protobuf names, enum/type identity and serialized bytes;
- storage key encodings, cache salts/identity tags, offsets, checkpoints and
  adapter/native extension identifiers;
- module discovery order and configured dependency overrides. Migrating a
  module can change `__module__`, which participates in sort/introspection;
  capture and compare actual order rather than assuming renames are harmless.

Changing a class's defining module may change pickle or persisted type names.
Legacy exports support loading old qualified names but do not automatically
preserve newly emitted bytes. Inspect actual persistence/plugin/trace consumers;
leave shared wire types with their owners and add golden-byte/load fixtures for
persisted identities. Any required representation change needs explicit review.
Do not blindly rewrite `__module__` across all moved classes.

Module/adapter/router discovery must scan the chosen canonical implementation
roots and deduplicate explicit compatibility imports. Test old-first and
new-first imports in fresh processes. Factory registries, router objects,
singletons and process-local owner counters must not be initialized twice.

## Validation plan

### Every implementation PR

1. Name moved symbols, excluded symbols, changed callers and prerequisite SHAs.
2. Retain original behavioral tests; add old/new import identity and caller
   coverage. Do not replace established suites with import-only tests.
3. Check the migrated boundary using AST imports, including relative imports,
   package initializers, local imports and `TYPE_CHECKING`. Static analysis is
   supplemented by fresh-process import blocking of the opposite endpoint.
4. Load server/L2 plugins through existing public paths, confirming config and
   interface identity, lazy dependencies and exactly-once registration.
5. Run `pre-commit run --all-files`, relevant focused regression suites and
   required native generation/build checks. DCO-sign every commit.
6. Open draft PRs with current validation evidence and the repository-required
   reviewer HTML; the submitting human reviews every changed line before ready.

Do not apply a global no-legacy-import rule to the still-mixed tree on day one.
Track explicitly reviewed temporary legacy contract edges with their external
owner. Remove them as predecessors land; the final gate has no temporary
cache-server-owned implementation left in legacy modules.

### Existing regression anchors

- Storage: `tests/v1/distributed/test_distributed_storage_manager.py`,
  `test_l1_manager.py`, `test_l1_manager_factory.py`, adapter/factory suites,
  `test_quota_manager.py`, eviction-policy and cache-salt eviction tests.
- Execution/extensions: `tests/v1/multiprocess/test_session.py`,
  `test_engine_module.py`, `test_ext_server_module.py`,
  `test_server_module_affinity_call.py` and corresponding transfer/lookup tests.
- HTTP: `tests/v1/multiprocess/test_http_server.py`,
  `test_http_api_registry.py`, `test_http_quota_endpoints.py`,
  `tests/v1/multiprocess/http_apis/`, `tests/v1/test_http_auth.py` and CLI tests.
- End to end: `tests/v1/multiprocess/test_cache_server.py` includes real
  registration, store/lookup and store/retrieve verification; these are not
  equivalent to mocked imports and can require accelerator resources.
- Cross-service: coordinator quota/eviction tests protect the shared-helper
  boundary while #6313 migrates that service.

Use `--noconftest` only for self-contained CPU identity/boundary tests. Normal
GPU integration tests retain their required fixtures. Report passed/skipped/
failed/unrun outcomes separately; CPU-only validation is not GPU readiness.

### Final #6311 acceptance matrix

| Acceptance | Completion evidence |
| --- | --- |
| Canonical ownership | Every cache-server-owned symbol in the inventory has a canonical service implementation; legacy locations contain explicit supported facades, not duplicate implementations |
| Boundary | No client/SDK/integration/platform path reaches cache-server implementation, including through old facades/type-only imports; common/shared transport remain endpoint-neutral |
| Compatibility | Old/new imports have identity, CLI and plugin cases work, HTTP/OpenAPI and auth/error behavior match, RPC/wire/key/persistence fixtures remain compatible |
| Storage | Allocation, locks, TTL, quotas, adapter lifecycle, prefetch/eviction, cleanup and result accounting suites pass |
| Runtime | Real store/lookup/retrieve verifies data; repeated requests demonstrate cache reuse without changed semantics; required devices/backends are explicitly accounted for |
| Full product | Full-wheel service entry points and package data remain available; client-wheel extraction is handed to #6315 rather than claimed here |
| Contribution | All required checks pass, every commit is DCO-signed, PRs are draft until human review, dependency PRs are merged or explicitly ordered |

## Branch and issue workflow

Maintain a migration ledger as the series lands: each cache-server-owned symbol
records its old path, canonical path, legacy export, changed consumers, owning
PR and validation evidence. Shared/excluded symbols record their other issue
owner and agreed destination. A directory rename alone is not a completed row.
The final audit must account for every entry, including optional backends,
runtime plugins, module entry points, package data and shutdown paths.

This branch carries the plan first. Create each implementation branch from the
then-current upstream dev after its prerequisites land. Where a genuine stack
is necessary, state its parent and rebase onto dev once the parent merges; do
not submit the whole issue as a single accumulated PR or copy other owners'
unfinished code to make a branch appear self-contained.

Each partial PR uses `Refs LMCache/LMCache#6311` and records the responsibility
completed. Do not put `Fixes #6311` on the first partial PR. Close the issue only
when the full inventory and final acceptance matrix have been verified.

The pasted tracking discussion asks contributors to coordinate before starting
and warns that this is a major architecture refactor. This local design and
branch are preparation, not evidence of assignment or maintainer approval. Post
the ownership proposal/design for coordination; do not publish comments or open
implementation PRs on the user's behalf without that instruction.
