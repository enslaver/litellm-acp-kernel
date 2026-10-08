# TODO

The proxy currently exposes the kernel's `compress`, `decompress`, `search_context` and `acp_status` tools; `decompress` returns originals only when the client resends them. These items bring it closer to the full [acp-kernel](https://github.com/ranxianglei/acp-kernel) feature set described in the paper. Names and signatures come from the kernel README; verify against the pinned kernel version before starting.

## 1. CCR: lossless tool-result offload

Replace large tool results with a small placeholder and let the model fetch the original on demand. This is the lossless alternative to lossy summaries.

- [ ] Pass `contentStore` into and out of `processTurn` per session. Add it to `_Session` in `hook.py` and make sure it is covered by the per-session lock.
- [ ] Enable it via config (`ccr: { enabled: true, ... }` through `ACP_KERNEL_CONFIG`), off by default.
- [ ] Expose the `acp_retrieve` tool and wrap `core.retrieve(contentStore, ref, { exportDir })`.
- [ ] Decide how large originals are returned. The kernel exports originals over `ccr.retrieveInlineTokens` (default 4000) to a file and returns a pointer; the proxy has no file-read tool for the client's model, so either inline them regardless of size or document the limit.
- [ ] Add `ACP_KERNEL_EXPORT_DIR`, with a safe default and cleanup of exported files when a session expires.
- [ ] Memory: the content store holds full tool outputs. Bound it (per-session size cap, eviction with the existing session LRU).
- [ ] Tests: store at arrival, retrieve inline, retrieve oversized, store survives across turns, isolation between sessions and API keys.

## 2. Supporting work

- [ ] Persist session state (summaries, content store) to disk or Redis so it survives proxy restarts and works across multiple workers. State is in memory per process today.
- [ ] Add a benchmark script that replays a recorded conversation with and without the callback and reports prompt tokens, cost and cache-read share, so the README can cite measurements for this proxy rather than the paper's.
