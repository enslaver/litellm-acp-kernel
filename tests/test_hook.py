import json
import os
import re
import shutil
from typing import Any, List

import pytest
from litellm.proxy._types import UserAPIKeyAuth
from litellm.types.utils import (
    ChatCompletionDeltaToolCall,
    ChatCompletionMessageToolCall,
    Choices,
    Delta,
    Function,
    Message,
    ModelResponse,
    ModelResponseStream,
    StreamingChoices,
)

from litellm_acp_kernel.hook import _PROXY_AcpKernelHandler
from litellm_acp_kernel.sidecar_client import AcpKernelSidecar

KERNEL_DIR = os.environ.get("ACP_KERNEL_DIR", "")
MODEL = "acp-test"

pytestmark = pytest.mark.skipif(
    not (KERNEL_DIR and shutil.which("node") and os.path.exists(os.path.join(KERNEL_DIR, "dist", "index.js"))),
    reason="needs ACP_KERNEL_DIR pointing at a built acp-kernel and node on PATH",
)

FILLER = "lorem ipsum dolor sit amet " * 300

REF_PATTERN = re.compile(r"<acp[^>]*>(m\d{5})</acp>")


def make_conversation(turns: int = 4) -> List[dict]:
    messages: List[dict] = [
        {"role": "system", "content": "You are a test assistant."},
        {"role": "user", "content": "Start of the task."},
    ]
    for i in range(turns):
        messages.append({"role": "assistant", "content": f"working on step {i}"})
        messages.append({"role": "user", "content": f"remember KEY_{i} and continue. {FILLER}"})
    return messages


def refs_in(messages: List[dict], marker: str) -> List[str]:
    found = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str) and marker in content:
            found.extend(REF_PATTERN.findall(content))
    return found


def compress_args(start: str, end: str) -> str:
    return json.dumps(
        {"content": [{"startId": start, "endId": end, "topic": "t", "summary": "SUMMARY_SENTINEL records that the user asked to remember the marker keys before the originals were dropped."}]}
    )


def tool_call_response(arguments: str, call_id: str = "call_1") -> ModelResponse:
    return ModelResponse(
        choices=[
            Choices(
                finish_reason="tool_calls",
                message=Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ChatCompletionMessageToolCall(
                            id=call_id, type="function", function=Function(name="compress", arguments=arguments)
                        )
                    ],
                ),
            )
        ]
    )


def text_response(text: str) -> ModelResponse:
    return ModelResponse(choices=[Choices(finish_reason="stop", message=Message(role="assistant", content=text))])


def stream_chunk(delta: Delta, finish_reason: Any = None) -> ModelResponseStream:
    return ModelResponseStream(choices=[StreamingChoices(delta=delta, finish_reason=finish_reason, index=0)])


def tool_stream(arguments: str, call_id: str = "call_1") -> List[ModelResponseStream]:
    split = len(arguments) // 2
    first = Delta(
        role="assistant",
        content=None,
        tool_calls=[
            ChatCompletionDeltaToolCall(
                id=call_id, type="function", index=0, function=Function(name="compress", arguments=arguments[:split])
            )
        ],
    )
    second = Delta(
        content=None,
        tool_calls=[
            ChatCompletionDeltaToolCall(
                id=None, type="function", index=0, function=Function(name=None, arguments=arguments[split:])
            )
        ],
    )
    return [stream_chunk(first), stream_chunk(second), stream_chunk(Delta(content=None), "tool_calls")]


def text_stream(text: str) -> List[ModelResponseStream]:
    return [stream_chunk(Delta(role="assistant", content=text)), stream_chunk(Delta(content=None), "stop")]


async def as_async_iter(chunks: List[Any]):
    for chunk in chunks:
        yield chunk


class Harness:
    def __init__(self, handler: _PROXY_AcpKernelHandler):
        self.handler = handler
        self.upstream_requests: List[dict] = []
        self.next_response: Any = None

        async def fake_upstream(data: dict, prepared: dict, stream: bool) -> Any:
            self.upstream_requests.append({"messages": prepared["messages"], "stream": stream})
            return as_async_iter(self.next_response) if stream else self.next_response

        handler._upstream = fake_upstream


@pytest.fixture
async def harness():
    handler = _PROXY_AcpKernelHandler(
        sidecar=AcpKernelSidecar(KERNEL_DIR), models={MODEL}, context_limit=4000, max_rounds=2
    )
    yield Harness(handler)
    await handler.sidecar.close()


def request_data(messages: List[dict], call_id: str, session: str = "s1", stream: bool = False) -> dict:
    return {
        "model": MODEL,
        "messages": messages,
        "stream": stream,
        "litellm_call_id": call_id,
        "proxy_server_request": {"headers": {"x-acp-session": session}},
    }


async def prepare(harness: Harness, messages: List[dict], call_id: str, token: str = "k1", **kw: Any) -> dict:
    data = request_data(messages, call_id, **kw)
    out = await harness.handler.async_pre_call_hook(UserAPIKeyAuth(token=token), None, data, "acompletion")
    assert out is not None
    return out


async def test_pre_call_injects_compress_tool_and_tags_messages(harness):
    data = await prepare(harness, make_conversation(), "c1")
    assert any(t["function"]["name"] == "compress" for t in data["tools"])
    assert len(refs_in(data["messages"], "KEY_")) >= 2
    assert data["messages"][0]["role"] == "system"
    assert "You are a test assistant." in data["messages"][0]["content"]


async def test_pre_call_ignores_other_models_and_call_types(harness):
    data = request_data(make_conversation(), "c1")
    data["model"] = "other"
    assert await harness.handler.async_pre_call_hook(UserAPIKeyAuth(), None, data, "acompletion") is None
    data["model"] = MODEL
    assert await harness.handler.async_pre_call_hook(UserAPIKeyAuth(), None, data, "aembedding") is None


async def test_compress_tool_call_is_applied_and_request_replayed_without_originals(harness):
    conversation = make_conversation()
    data = await prepare(harness, conversation, "c1")
    refs = refs_in(data["messages"], "KEY_")
    harness.next_response = text_response("final answer")

    result = await harness.handler.async_post_call_success_hook(
        data, UserAPIKeyAuth(token="k1"), tool_call_response(compress_args(refs[0], refs[1]))
    )

    assert result.choices[0].message.content == "final answer"
    replay = harness.upstream_requests[0]["messages"]
    flattened = "\n".join(str(m.get("content")) for m in replay)
    assert "SUMMARY_SENTINEL" in flattened
    assert not any("KEY_0" in str(m.get("content")) for m in replay)
    assert any("KEY_3" in str(m.get("content")) for m in replay)


async def test_compressed_state_persists_into_next_turn(harness):
    conversation = make_conversation()
    data = await prepare(harness, conversation, "c1")
    refs = refs_in(data["messages"], "KEY_")
    harness.next_response = text_response("ok")
    await harness.handler.async_post_call_success_hook(
        data, UserAPIKeyAuth(token="k1"), tool_call_response(compress_args(refs[0], refs[1]))
    )

    follow_up = conversation + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "next question"}]
    nxt = await prepare(harness, follow_up, "c2")
    flattened = "\n".join(str(m.get("content")) for m in nxt["messages"])
    assert "SUMMARY_SENTINEL" in flattened
    assert "KEY_0" not in flattened


async def test_invalid_range_feeds_error_back_to_model(harness):
    data = await prepare(harness, make_conversation(), "c1")
    harness.next_response = text_response("retrying")

    await harness.handler.async_post_call_success_hook(
        data, UserAPIKeyAuth(token="k1"), tool_call_response(compress_args("m99998", "m99999"), call_id="bad_1")
    )

    replay = harness.upstream_requests[0]["messages"]
    assert replay[-1]["role"] == "tool"
    assert replay[-1]["tool_call_id"] == "bad_1"
    assert replay[-1]["content"].startswith("compress failed:")


async def test_response_without_compress_call_passes_through(harness):
    data = await prepare(harness, make_conversation(), "c1")
    response = text_response("plain")
    assert await harness.handler.async_post_call_success_hook(data, UserAPIKeyAuth(token="k1"), response) is response
    assert harness.upstream_requests == []


async def test_sessions_are_isolated_per_api_key(harness):
    first = make_conversation()
    data_a = await prepare(harness, first, "c1", token="tenant-a")
    refs = refs_in(data_a["messages"], "KEY_")
    harness.next_response = text_response("ok")
    await harness.handler.async_post_call_success_hook(
        data_a, UserAPIKeyAuth(token="tenant-a"), tool_call_response(compress_args(refs[0], refs[1]))
    )

    data_b = await prepare(harness, first, "c2", token="tenant-b")
    flattened = "\n".join(str(m.get("content")) for m in data_b["messages"])
    assert "SUMMARY_SENTINEL" not in flattened


async def test_streaming_compress_call_is_hidden_and_replaced_by_replay_stream(harness):
    data = await prepare(harness, make_conversation(), "c1", stream=True)
    refs = refs_in(data["messages"], "KEY_")
    harness.next_response = text_stream("streamed answer")

    chunks = [
        chunk
        async for chunk in harness.handler.async_post_call_streaming_iterator_hook(
            UserAPIKeyAuth(token="k1"), as_async_iter(tool_stream(compress_args(refs[0], refs[1]))), data
        )
    ]

    assert harness.upstream_requests[0]["stream"] is True
    assert "SUMMARY_SENTINEL" in "\n".join(str(m.get("content")) for m in harness.upstream_requests[0]["messages"])
    assert not any(getattr(c.choices[0].delta, "tool_calls", None) for c in chunks)
    assert not any(c.choices[0].finish_reason == "tool_calls" for c in chunks)
    assert "".join(c.choices[0].delta.content or "" for c in chunks) == "streamed answer"


async def test_streaming_text_response_passes_through_untouched(harness):
    data = await prepare(harness, make_conversation(), "c1", stream=True)
    source = text_stream("hello")
    chunks = [
        c
        async for c in harness.handler.async_post_call_streaming_iterator_hook(
            UserAPIKeyAuth(token="k1"), as_async_iter(source), data
        )
    ]
    assert chunks == source
    assert harness.upstream_requests == []


async def test_sidecar_recovers_after_crash(harness):
    await prepare(harness, make_conversation(), "c1")
    harness.handler.sidecar._proc.kill()
    await harness.handler.sidecar._proc.wait()
    data = await prepare(harness, make_conversation(), "c2", session="s2")
    assert any(t["function"]["name"] == "compress" for t in data["tools"])
