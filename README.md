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

Other kernel behavior that helps:

- **Protected content** is filtered out of compression, so content that must stay verbatim isn't summarized away.
- **Emergency truncation** is a last resort when context is about to overflow.
- **Lossless offload (CCR)** can replace large tool results with a small placeholder and let the model fetch the original back on demand. It is opt-in in the kernel (via `ACP_KERNEL_CONFIG`) and off by default.
- **Decompress and search** let the model look a compressed block back up if it needs the detail.

### Trade-offs

- Writing a summary costs the model some output tokens, and a compression turn costs an extra upstream round. This pays back over the remaining turns of the session, so it matters most for long ones.
- Summaries are lossy. Quality depends on the model you route to; use the protected-content and CCR features for anything that must be exact.
- Rewriting history changes the prompt prefix at the moment of compression, so provider-side prompt caching resets for that turn. Between compressions the kernel's placeholders are deterministic, which keeps the prefix stable.
- Session state lives in memory per proxy process (see below).

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

Sessions are keyed by API key, model and `x-acp-session` header. State is in memory per process.

## Measuring savings

Compare token usage with the callback on and off for the same workload:

- LiteLLM logs `usage.prompt_tokens` for every request; the callback also records it per session. Sum prompt tokens per session with and without `ACP_KERNEL_MODELS` set for your model.
- Multiply by your provider's input price (LiteLLM's spend logs do this for you) to get the cost difference.
- Watch for requests that include a `compress` round: those are the ones that pay for a summary.

Measured benchmarks for this proxy aren't published yet; contributions are welcome.

## Test

    ACP_KERNEL_DIR=/path/to/kernel pytest tests

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities per [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
