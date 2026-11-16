# litellm-acp-kernel

[![CI](https://github.com/enslaver/litellm-acp-kernel/actions/workflows/ci.yml/badge.svg)](https://github.com/enslaver/litellm-acp-kernel/actions/workflows/ci.yml)
[![CodeQL](https://github.com/enslaver/litellm-acp-kernel/actions/workflows/codeql.yml/badge.svg)](https://github.com/enslaver/litellm-acp-kernel/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Cut the token bill on long-running LLM conversations and agent sessions, without changing your clients.**

`litellm-acp-kernel` is a [LiteLLM](https://github.com/BerriAI/litellm) proxy callback that plugs [acp-kernel](https://github.com/ranxianglei/acp-kernel), a model-driven context-compression engine, into `/v1/chat/completions` (streaming and non-streaming). Clients keep talking to the proxy exactly as before; the proxy keeps their context small.

## Why it saves tokens and money

An LLM API has no memory. Every turn re-sends the whole conversation, so you pay for the entire history again on each request. A session that grows by a few thousand tokens per turn doesn't cost "a few thousand per turn": its input cost grows with the *running total*, so it is roughly quadratic in the number of turns. Long agent loops full of tool output (file reads, command logs, search results) are the worst case.

Compression attacks that term directly. Once an old stretch of the conversation is replaced by a short summary, **every later request carries the summary instead of the original text.** The saving is not one-time, it repeats on every subsequent turn:

    saved tokens ≈ (original tokens − summary tokens) × remaining turns

That has three practical effects:

- **Lower input-token spend** on every turn after a compression, which is where most of the cost of long sessions sits.
- **More headroom before the context limit**, so sessions run longer without hitting `context_length_exceeded` or being truncated blindly.
- **Smaller prompts**, which also means lower latency and less distraction for the model from stale tool output.

Savings depend on your workload: tool-heavy agent sessions with large, one-off outputs compress far better than short chats. Measure on your own traffic; see [Measuring savings](#measuring-savings).

## How it works

The core design principle of acp-kernel is **the model writes the summaries; the kernel orchestrates everything around them.** The kernel never calls a model itself. It decides *when* to compress and *what range*, tracks state, and applies the result. The proxy wires that to your upstream model.

1. **Tag.** On each request the callback runs the kernel over the incoming messages. Every message gets a short reference (like `m00005`), the anchor the model uses to say which part of the conversation it means.
2. **Nudge.** When the conversation has grown enough that compressing is worthwhile, the kernel injects a nudge and a `compress` tool into the request. Nudges are growth-gated: they fire when there is real new compressible mass and context pressure, not on every turn.
3. **Model-written summary.** The model calls `compress` with a message range (`startRef` to `endRef`) and a summary it wrote itself. Because the model chooses the range and authors the summary, it keeps what matters for the task and drops what doesn't.
4. **Replace and replay.** The proxy hides the `compress` call from the client, applies the summary to the session state, replaces the original range with it, and replays the request upstream (up to `ACP_KERNEL_MAX_ROUNDS` times). The client sees an ordinary response, streamed or not.
5. **Compound.** Summaries are stored in a 3-tier LSM-style hierarchy. As summaries accumulate, the kernel nudges the model to merge them into denser ones, so history keeps shrinking instead of just being capped.

Other kernel behavior that applies here:

- **Protected zones.** The compress calls themselves, the recent zone, the first user message (task intent) and the last user message are never compressed. This is enforced by the kernel, not by prompt text.
- **Emergency truncation** is a last resort when context is about to overflow.
- **Window-scaled thresholds.** Nudge thresholds scale with `ACP_KERNEL_CONTEXT_LIMIT`, so set it to the real window of the model you route to.

The proxy also exposes `decompress`, `search_context` and `acp_status`. They are read-only lookups: `decompress` returns the original messages of a block (capped at 32K characters) in the tool result, so the folded prefix and its cache stay untouched. 
**Lossless offload (CCR), off by default.** Enable it with `ACP_KERNEL_CONFIG='{"ccr": {"enabled": true}}'`. Tool results above `ccr.minToolTokens` (default 4000) are stored per session and replaced in the request by a short placeholder with a ref; the model can call `acp_retrieve` to get the exact original back in the tool result. Originals are always returned inline (no export directory), so a retrieval of a large output re-adds those tokens for that turn. The store lives in proxy memory with the session and is not size-capped yet; see [TODO.md](TODO.md).

### Trade-offs

- **Overhead per request.** The compression doctrine adds roughly 2K tokens of system prompt to every request. In the paper's 16K-window test that was about a 25% fixed overhead and one-shot compaction was cheaper; the overhead amortizes at 64K+ windows. Use this for models with large windows and long sessions.
- **Compression turns cost extra.** The model writes a summary (output tokens) and the proxy replays the request upstream. The paper measures summary output at a median of 185 tokens, so this pays back quickly, but it is not free.
- **Summaries are lossy.** Quality depends on the model you route to, and `decompress` can only restore content the client still sends, since the proxy keeps no copy of the originals. Tasks that need verbatim history (audits, compliance, forensics) are outside what this approach is meant for.
- **Cache.** Compression replaces a range of history, so provider-side prompt cache is lost from the start of that range onward. The kernel folds the already-consumed increment rather than rewriting the whole history, which keeps the earlier prefix stable; the paper reports 94% cache-read share at active tempo on its own hosts. This proxy has not measured that yet.
- **Session state is in memory** per proxy process unless `ACP_KERNEL_STATE_DIR` is set (see below).

## Install

    pip install litellm-acp-kernel

Then build [acp-kernel](https://github.com/ranxianglei/acp-kernel) (`npm ci && npm run build`) and point `ACP_KERNEL_DIR` at it.

## Configure

    litellm_settings:
      callbacks: ["litellm_acp_kernel.acp_kernel_handler"]

Environment:

- `ACP_KERNEL_DIR`: path to the built acp-kernel (required)
- `ACP_KERNEL_MODELS`: comma-separated `model_name` values to compress (required; others pass through)
- `ACP_KERNEL_CONTEXT_LIMIT`: token budget, default 128000
- `ACP_KERNEL_MAX_ROUNDS`: max compress replays per request, default 3
- `ACP_KERNEL_CONFIG`: JSON overrides for the kernel config
- `ACP_KERNEL_NODE`: node binary, default from PATH
- `ACP_KERNEL_STATE_DIR`: persist session state (summaries and, with CCR, stored tool outputs) here so it survives proxy restarts. Off by default. Files can contain conversation content: the directory is created `0700`, so keep it private.
- `ACP_KERNEL_STATE_TTL_DAYS`: files older than this are deleted at startup, default 7
- `ACP_KERNEL_STATE_DEBOUNCE_MS`: write coalescing window, default 500. A crash can lose up to this much recent state.

Sessions are keyed by API key, model and `x-acp-session` header. State is held in memory per process; with `ACP_KERNEL_STATE_DIR` it is also written to disk (via the kernel's crash-safe `StateStore`) and reloaded when a session is not in memory, e.g. after a restart or LRU eviction. Several workers sharing one directory is last-writer-wins and is not coordinated, so pin a session to one worker.

## Evidence

The compression method is the one described in [*Model-Driven Incremental Hierarchical Compression*](https://github.com/ranxianglei/billion-context/blob/master/paper/model-driven-incremental-hierarchical-compression-training-free-multi-generational-context-management-for-long-lived-coding-agents.md) (Ran, preprint v0.2, 2026-09-07), whose reference system runs on acp-kernel. Numbers below are the paper's, self-reported by its author from their own deployments and a small pilot. **They were not measured on this LiteLLM callback**, and the paper's own caveats apply.

Controlled pilot (Qwen3.8-27B, 65,536-token window, synthetic multi-task coding sessions, compared against a sliding window that keeps full history):

| Workload | Billed tokens vs sliding window | Recall probes |
|---|---|---|
| Repetitive, 6 passes | 31% fewer (default tuning), 54% fewer (aggressive tuning) | Equal under default tuning |
| Phase-structured migration, 3 seeds | 50% fewer (4.67M vs 9.40M), lowest variance of any arm | 81.6% vs 80.9% |
| Task-order permutations, 3 seeds | 52–53% fewer, 4–7× lower variance | Equal or better |

Production use (author's own daily-driver hosts, up to 4.5 months):

| Measure | Result |
|---|---|
| Models with a 204,800-token window | 42,986 calls, zero window overflows, peak 198,628 tokens |
| Typical per-call context (Pi host) | mean 88K, median 72K tokens |
| Cumulative input processed | 18.76B tokens on the main host over 4.5 months, in sessions up to 12,049 calls |
| Per-block compression | median 7.9×; tool-heavy blocks median 24× |
| Summary size | median 185 tokens, 97.5% of summaries ≤ 2K tokens |
| Heavy sessions | about 70× standing compression (~900K tokens of history held as ~13K of summaries) |
| Cache-read share | 94.2% at active tempo, 90.7% blended |

Where it helps most, per the paper's own model: on a 1M-token window, a long session's input is modeled at about 5.3× lower than threshold-compaction would give. On 200K windows the modeled raw saving is smaller, 1.2–1.8×, and the main benefit is that sessions never overflow and can run indefinitely. The paper notes that compression activity is concentrated in marathon sessions: 62% of compressed tokens came from the 1.3% of sessions with 1,000+ messages.

Caveats stated by the paper: single-expert production data, a small pilot on one model, probe scores near the suite's ceiling, and an ablation showing that most of the re-fetch reduction (−62% of −63%) comes from the doctrine text plus message tags rather than the compression itself.

## Measuring savings on your own traffic

- LiteLLM logs `usage.prompt_tokens` per request; the callback records it per session. Compare per-session prompt tokens for the same workload with and without your model in `ACP_KERNEL_MODELS`.
- Multiply by your provider's input price (LiteLLM's spend logs do this) for the cost difference. If your provider discounts cached input, compare cached and uncached tokens separately.
- Requests that include a `compress` round are the ones that pay for a summary.

To automate this, `scripts/benchmark.py` replays a recorded conversation against two models on your proxy (the same upstream model with and without the callback) and prints prompt tokens, cache-read share and optional cost:

    python scripts/benchmark.py conversation.json --baseline gpt-4o --acp gpt-4o-acp --price-in 2.5 --price-out 10

The conversation is a JSON list of OpenAI-style messages. Recorded assistant replies are sent as history so both arms see identical input; live replies are discarded.

Published measurements for this proxy are welcome as a PR.

## A note on the name

ACP here means Active Context Pruning, the kernel's name. It is unrelated to the Agent Client Protocol or the Agent Communication Protocol.

## Test

    ACP_KERNEL_DIR=/path/to/kernel pytest tests

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities per [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
