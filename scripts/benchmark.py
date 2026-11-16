#!/usr/bin/env python3
"""Replay a recorded conversation against two models on a LiteLLM proxy and compare token usage.

One model is routed through the acp-kernel callback (ACP_KERNEL_MODELS), the other is the same
upstream model without it. For every assistant turn in the recording, the history before that turn
is sent to both and the reported usage is summed.

    python scripts/benchmark.py conversation.json --base-url http://localhost:4000 \
        --baseline gpt-4o --acp gpt-4o-acp --api-key sk-... --price-in 2.5 --price-out 10

conversation.json is a JSON list of OpenAI-style messages. The recorded assistant replies are sent
as history; the live replies are discarded, so both arms see identical input.
"""
import argparse
import json
import sys
import urllib.request
from typing import Any, Dict, List


def usage_of(response: Dict[str, Any]) -> Dict[str, int]:
    usage = response.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt": int(usage.get("prompt_tokens") or 0),
        "cached": int(details.get("cached_tokens") or 0),
        "completion": int(usage.get("completion_tokens") or 0),
    }


def turn_boundaries(messages: List[Dict[str, Any]]) -> List[int]:
    """Indexes of assistant messages: the history before each one is a request."""
    return [i for i, m in enumerate(messages) if m.get("role") == "assistant" and i > 0]


def run_arm(args: argparse.Namespace, model: str, messages: List[Dict[str, Any]], session: str) -> Dict[str, int]:
    totals = {"prompt": 0, "cached": 0, "completion": 0, "requests": 0}
    for index in turn_boundaries(messages):
        payload = {"model": model, "messages": messages[:index], "max_tokens": args.max_tokens}
        request = urllib.request.Request(
            args.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {args.api_key}",
                "x-acp-session": session,
            },
        )
        with urllib.request.urlopen(request, timeout=300) as handle:
            used = usage_of(json.load(handle))
        for key, value in used.items():
            totals[key] += value
        totals["requests"] += 1
    return totals


def cost(totals: Dict[str, int], price_in: float, price_out: float) -> float:
    return (totals["prompt"] * price_in + totals["completion"] * price_out) / 1_000_000


def report(name: str, totals: Dict[str, int], args: argparse.Namespace) -> None:
    share = totals["cached"] / totals["prompt"] if totals["prompt"] else 0.0
    line = (
        f"{name:10} requests={totals['requests']} prompt={totals['prompt']} "
        f"completion={totals['completion']} cache_read_share={share:.1%}"
    )
    if args.price_in or args.price_out:
        line += f" cost=${cost(totals, args.price_in, args.price_out):.4f}"
    print(line)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("conversation", help="JSON file: list of OpenAI-style messages")
    parser.add_argument("--base-url", default="http://localhost:4000", help="LiteLLM proxy base URL (OpenAI-compatible)")
    parser.add_argument("--baseline", required=True, help="model name without the callback")
    parser.add_argument("--acp", required=True, help="model name routed through the callback")
    parser.add_argument("--api-key", default="sk-1234")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--price-in", type=float, default=0.0, help="USD per 1M prompt tokens")
    parser.add_argument("--price-out", type=float, default=0.0, help="USD per 1M completion tokens")
    args = parser.parse_args(argv)

    with open(args.conversation) as handle:
        messages = json.load(handle)
    if not turn_boundaries(messages):
        print("conversation has no assistant turns to replay", file=sys.stderr)
        return 2

    baseline = run_arm(args, args.baseline, messages, "bench-baseline")
    acp = run_arm(args, args.acp, messages, "bench-acp")
    report("baseline", baseline, args)
    report("acp", acp, args)
    if baseline["prompt"]:
        print(f"prompt tokens vs baseline: {acp['prompt'] / baseline['prompt'] - 1:+.1%}")
    if args.price_in or args.price_out:
        base_cost = cost(baseline, args.price_in, args.price_out)
        if base_cost:
            print(f"cost vs baseline: {cost(acp, args.price_in, args.price_out) / base_cost - 1:+.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
