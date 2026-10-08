# TODO

The proxy currently exposes the kernel's `compress`, `decompress`, `search_context` and `acp_status` tools, plus `acp_retrieve` when CCR is enabled; `decompress` returns originals only when the client resends them. These items bring it closer to the full [acp-kernel](https://github.com/ranxianglei/acp-kernel) feature set described in the paper. Names and signatures come from the kernel README; verify against the pinned kernel version before starting.

## 1. CCR follow-ups

CCR itself is implemented (opt-in, inline retrieval).

- [ ] Bound the content store (per-session size cap, eviction with the session LRU). It currently grows with every stored tool result.
- [ ] Optional export directory for very large originals, if a file-read path for the client's model exists.

## 2. Supporting work

- [ ] Persist session state (summaries, content store) to disk or Redis so it survives proxy restarts and works across multiple workers. State is in memory per process today.
- [ ] Add a benchmark script that replays a recorded conversation with and without the callback and reports prompt tokens, cost and cache-read share, so the README can cite measurements for this proxy rather than the paper's.
