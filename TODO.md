# TODO

The proxy currently exposes the kernel's `compress`, `decompress`, `search_context` and `acp_status` tools, plus `acp_retrieve` when CCR is enabled; `decompress` returns originals only when the client resends them. These items bring it closer to the full [acp-kernel](https://github.com/ranxianglei/acp-kernel) feature set described in the paper. Names and signatures come from the kernel README; verify against the pinned kernel version before starting.

## 1. CCR follow-ups

CCR itself is implemented (opt-in, inline retrieval).

- [ ] Bound the content store (per-session size cap, eviction with the session LRU). It currently grows with every stored tool result.
- [ ] Optional export directory for very large originals, if a file-read path for the client's model exists.

## 2. Supporting work

- [ ] Coordinate persisted state across multiple workers (Redis or locking). Single-process persistence is done via `ACP_KERNEL_STATE_DIR`.
