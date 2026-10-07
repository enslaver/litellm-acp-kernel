# litellm-acp-kernel

[![CI](https://github.com/enslaver/litellm-acp-kernel/actions/workflows/ci.yml/badge.svg)](https://github.com/enslaver/litellm-acp-kernel/actions/workflows/ci.yml)
[![CodeQL](https://github.com/enslaver/litellm-acp-kernel/actions/workflows/codeql.yml/badge.svg)](https://github.com/enslaver/litellm-acp-kernel/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

LiteLLM proxy callback that adds [acp-kernel](https://github.com/ranxianglei/acp-kernel) model-driven context compression to `/v1/chat/completions` (streaming and non-streaming). The kernel runs unmodified in a managed Node sidecar. Requires Node 18+ and a built kernel checkout.

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

## Test

    ACP_KERNEL_DIR=/path/to/kernel pytest tests

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities per [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
