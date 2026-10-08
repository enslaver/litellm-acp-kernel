# TODO

The proxy currently exposes only the kernel's `compress` tool. These items bring it closer to the full [acp-kernel](https://github.com/ranxianglei/acp-kernel) feature set described in the paper. Names and signatures come from the kernel README; verify against the pinned kernel version before starting.

## 1. `decompress`: restore a compressed block

Let the model bring a summarized block back when it needs the detail.

- [ ] Add a sidecar op wrapping `core.decompress(blockId, state)` and return the updated state.
- [ ] Inject a decompress tool schema next to `COMPRESS_TOOL_OPENAI` in `withCompressTool` (`sidecar.mjs`).
- [ ] Handle the tool call in `hook.py` the way `compress` is handled: intercept it in the non-streaming and streaming hooks, apply it, and replay upstream, bounded by `ACP_KERNEL_MAX_ROUNDS`.
- [ ] Prefer the kernel's decompress-to-file mode so the restored text does not disturb the cached prefix. Needs a host-managed export directory (see CCR) and a safe file-read path for the model.
- [ ] Tests: restore after compress, unknown block id, restore across streaming and non-streaming, tool-call mixed with the client's own tool calls.

## 2. `search_context`: search summaries and recent messages

Lets the model find which block holds a detail before restoring it.

- [ ] Add a sidecar op wrapping `core.search(query, state)`.
- [ ] Expose the tool schema and route its calls through the same intercept-and-replay path as `compress`.
- [ ] Consider also exposing `acp_status` (`core.status`) for context-usage reporting.
- [ ] Tests: ranking, empty result, interaction with decompress.

## 3. CCR: lossless tool-result offload

Replace large tool results with a small placeholder and let the model fetch the original on demand. This is the lossless alternative to lossy summaries.

- [ ] Pass `contentStore` into and out of `processTurn` per session. Add it to `_Session` in `hook.py` and make sure it is covered by the per-session lock.
- [ ] Enable it via config (`ccr: { enabled: true, ... }` through `ACP_KERNEL_CONFIG`), off by default.
- [ ] Expose the `acp_retrieve` tool and wrap `core.retrieve(contentStore, ref, { exportDir })`.
- [ ] Decide how large originals are returned. The kernel exports originals over `ccr.retrieveInlineTokens` (default 4000) to a file and returns a pointer; the proxy has no file-read tool for the client's model, so either inline them regardless of size or document the limit.
- [ ] Add `ACP_KERNEL_EXPORT_DIR`, with a safe default and cleanup of exported files when a session expires.
- [ ] Memory: the content store holds full tool outputs. Bound it (per-session size cap, eviction with the existing session LRU).
- [ ] Tests: store at arrival, retrieve inline, retrieve oversized, store survives across turns, isolation between sessions and API keys.

## 4. Supporting work

- [ ] Persist session state (summaries, content store) to disk or Redis so it survives proxy restarts and works across multiple workers. State is in memory per process today.
- [ ] Update the README "Not exposed through this proxy yet" note as each item lands.
