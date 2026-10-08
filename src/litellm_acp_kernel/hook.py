import asyncio
import hashlib
import json
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional, Set

import litellm
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

from litellm_acp_kernel.sidecar_client import AcpKernelSidecar

COMPRESS_TOOL_NAME = "compress"
INFO_TOOL_NAMES = frozenset({"decompress", "search_context", "acp_status", "acp_retrieve"})
HIDDEN_TOOL_NAMES = INFO_TOOL_NAMES | {COMPRESS_TOOL_NAME}
SESSION_HEADER = "x-acp-session"
DEFAULT_CONTEXT_LIMIT = 128_000
DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_SESSIONS = 1000
MAX_INFLIGHT_REQUESTS = 4096
INTERNAL_REQUEST_KEYS = frozenset(
    {"litellm_call_id", "litellm_logging_obj", "litellm_trace_id", "proxy_server_request", "messages", "tools"}
)


@dataclass
class _Session:
    state: Optional[dict] = None
    content_store: Optional[dict] = None
    last_prompt_tokens: Optional[int] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _Original:
    session_key: str
    body: Dict[str, Any]


@dataclass
class _StreamOutcome:
    compress_calls: Dict[int, Dict[str, str]] = field(default_factory=dict)
    has_other_tool_calls: bool = False


class _PROXY_AcpKernelHandler(CustomLogger):
    def __init__(
        self,
        sidecar: AcpKernelSidecar,
        models: Set[str],
        context_limit: int = DEFAULT_CONTEXT_LIMIT,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        config_overrides: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        self.sidecar = sidecar
        self.models = models
        self.context_limit = context_limit
        self.max_rounds = max_rounds
        self.max_sessions = max_sessions
        self.config_overrides = config_overrides or {}
        self._sessions: "OrderedDict[str, _Session]" = OrderedDict()
        self._inflight: "OrderedDict[str, _Original]" = OrderedDict()

    @classmethod
    def from_env(cls) -> "_PROXY_AcpKernelHandler":
        kernel_dir = os.environ.get("ACP_KERNEL_DIR", "")
        models = {m.strip() for m in os.environ.get("ACP_KERNEL_MODELS", "").split(",") if m.strip()}
        return cls(
            sidecar=AcpKernelSidecar(kernel_dir),
            models=models,
            context_limit=int(os.environ.get("ACP_KERNEL_CONTEXT_LIMIT", DEFAULT_CONTEXT_LIMIT)),
            max_rounds=int(os.environ.get("ACP_KERNEL_MAX_ROUNDS", DEFAULT_MAX_ROUNDS)),
            config_overrides=json.loads(os.environ.get("ACP_KERNEL_CONFIG", "{}")),
        )

    async def async_pre_call_hook(
        self, user_api_key_dict: UserAPIKeyAuth, cache: Any, data: dict, call_type: Any
    ) -> Optional[dict]:
        if call_type != "acompletion" or data.get("model") not in self.models:
            return None
        call_id = data.get("litellm_call_id")
        if not call_id:
            return None
        session_key = self._session_key(user_api_key_dict, data)
        body = {"messages": data.get("messages") or [], "tools": data.get("tools")}
        prepared = await self._prepare(session_key, body, inject_nudge=True)
        self._remember(call_id, _Original(session_key=session_key, body=body))
        data["messages"] = prepared["messages"]
        data["tools"] = prepared["tools"]
        return data

    async def async_post_call_success_hook(self, data: dict, user_api_key_dict: UserAPIKeyAuth, response: Any) -> Any:
        original = self._inflight.pop(data.get("litellm_call_id", ""), None)
        if original is None:
            return None
        self._record_usage(original.session_key, getattr(response, "usage", None))
        for _ in range(self.max_rounds):
            message = _first_message(response)
            calls = _hidden_calls_from_message(message)
            if not calls:
                return response
            others = [tc for tc in (message.tool_calls or []) if tc.function.name not in HIDDEN_TOOL_NAMES]
            prepared = await self._apply_and_prepare(original, calls)
            if others:
                message.tool_calls = others
                return response
            response = await self._upstream(data, prepared, stream=False)
            self._record_usage(original.session_key, getattr(response, "usage", None))
        return response

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: UserAPIKeyAuth, response: Any, request_data: dict
    ) -> AsyncGenerator[Any, None]:
        original = self._inflight.pop(request_data.get("litellm_call_id", ""), None)
        if original is None:
            async for chunk in response:
                yield chunk
            return
        source = response
        for round_no in range(self.max_rounds + 1):
            outcome = _StreamOutcome()
            final_round = round_no == self.max_rounds
            async for chunk in self._filter_stream(source, original, outcome, passthrough=final_round):
                yield chunk
            if not outcome.compress_calls:
                return
            calls = list(outcome.compress_calls.values())
            prepared = await self._apply_and_prepare(original, calls)
            if outcome.has_other_tool_calls:
                return
            source = await self._upstream(request_data, prepared, stream=True)

    async def _filter_stream(
        self, source: Any, original: _Original, outcome: _StreamOutcome, passthrough: bool
    ) -> AsyncGenerator[Any, None]:
        async for chunk in source:
            self._record_usage(original.session_key, getattr(chunk, "usage", None))
            if passthrough or not chunk.choices:
                yield chunk
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            tool_calls = getattr(delta, "tool_calls", None)
            if tool_calls:
                kept = []
                for tc in tool_calls:
                    fn = tc.function
                    index = tc.index if tc.index is not None else 0
                    if fn is not None and fn.name in HIDDEN_TOOL_NAMES:
                        outcome.compress_calls[index] = {"id": tc.id or "", "name": fn.name, "arguments": fn.arguments or ""}
                    elif index in outcome.compress_calls:
                        entry = outcome.compress_calls[index]
                        entry["arguments"] += fn.arguments if fn is not None and fn.arguments else ""
                        entry["id"] = entry["id"] or tc.id or ""
                    else:
                        kept.append(tc)
                        outcome.has_other_tool_calls = True
                if len(kept) != len(tool_calls):
                    delta.tool_calls = kept or None
                    if not kept and not delta.content and not getattr(delta, "role", None):
                        continue
            if choice.finish_reason == "tool_calls" and outcome.compress_calls and not outcome.has_other_tool_calls:
                continue
            yield chunk

    async def _apply_and_prepare(self, original: _Original, calls: List[Dict[str, str]]) -> dict:
        session_key = original.session_key
        session = self._session(session_key)
        compress_calls = [c for c in calls if c["name"] == COMPRESS_TOOL_NAME]
        info_calls = [c for c in calls if c["name"] in INFO_TOOL_NAMES]
        async with session.lock:
            applied = {"state": session.state, "results": []}
            if compress_calls:
                applied = await self.sidecar.call(
                    "apply",
                    body=original.body,
                    state=session.state,
                    calls=compress_calls,
                    contextLimit=self.context_limit,
                    config=self.config_overrides,
                )
                session.state = applied["state"]
            looked_up = {"results": []}
            if info_calls:
                looked_up = await self.sidecar.call(
                    "tool",
                    body=original.body,
                    state=session.state,
                    contentStore=session.content_store,
                    calls=info_calls,
                    contextLimit=self.context_limit,
                    config=self.config_overrides,
                    tokenCount=session.last_prompt_tokens,
                )
            prepared = await self.sidecar.call(
                "prepare",
                body=original.body,
                state=session.state,
                contentStore=session.content_store,
                sessionKey=session_key,
                contextLimit=self.context_limit,
                config=self.config_overrides,
                tokenCount=None,
                injectNudge=False,
            )
            session.state = prepared["state"]
            session.content_store = prepared["contentStore"]
        feedback = _failure_feedback(compress_calls, applied["results"]) + _tool_results(info_calls, looked_up["results"])
        prepared["messages"] = prepared["messages"] + feedback
        return prepared

    async def _prepare(self, session_key: str, body: dict, inject_nudge: bool) -> dict:
        session = self._session(session_key)
        async with session.lock:
            prepared = await self.sidecar.call(
                "prepare",
                body=body,
                state=session.state,
                contentStore=session.content_store,
                sessionKey=session_key,
                contextLimit=self.context_limit,
                config=self.config_overrides,
                tokenCount=session.last_prompt_tokens,
                injectNudge=inject_nudge,
            )
            session.state = prepared["state"]
            session.content_store = prepared["contentStore"]
        return prepared

    async def _upstream(self, data: dict, prepared: dict, stream: bool) -> Any:
        from litellm.proxy import proxy_server

        kwargs = {k: v for k, v in data.items() if k not in INTERNAL_REQUEST_KEYS}
        kwargs.update(messages=prepared["messages"], tools=prepared["tools"], stream=stream)
        router = proxy_server.llm_router
        completion = router.acompletion if router is not None else litellm.acompletion
        return await completion(**kwargs)

    def _session(self, key: str) -> _Session:
        session = self._sessions.get(key)
        if session is None:
            session = _Session()
            self._sessions[key] = session
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
        else:
            self._sessions.move_to_end(key)
        return session

    def _remember(self, call_id: str, original: _Original) -> None:
        self._inflight[call_id] = original
        while len(self._inflight) > MAX_INFLIGHT_REQUESTS:
            self._inflight.popitem(last=False)

    def _record_usage(self, session_key: str, usage: Any) -> None:
        prompt_tokens = getattr(usage, "prompt_tokens", None) if usage is not None else None
        if prompt_tokens:
            self._session(session_key).last_prompt_tokens = int(prompt_tokens)

    def _session_key(self, user_api_key_dict: UserAPIKeyAuth, data: dict) -> str:
        headers = (data.get("proxy_server_request") or {}).get("headers") or {}
        metadata = data.get("metadata") or {}
        session_id = (
            headers.get(SESSION_HEADER)
            or data.get("litellm_session_id")
            or metadata.get("session_id")
            or _conversation_fingerprint(data)
        )
        owner = getattr(user_api_key_dict, "token", None) or ""
        return hashlib.sha256(f"{owner}\x00{data.get('model')}\x00{session_id}".encode()).hexdigest()


def _conversation_fingerprint(data: dict) -> str:
    for message in data.get("messages") or []:
        if message.get("role") == "user":
            return hashlib.sha256(json.dumps(message.get("content"), sort_keys=True).encode()).hexdigest()
    return ""


def _first_message(response: Any) -> Any:
    choices = getattr(response, "choices", None)
    return choices[0].message if choices else None


def _hidden_calls_from_message(message: Any) -> List[Dict[str, str]]:
    tool_calls = getattr(message, "tool_calls", None) or []
    return [
        {"id": tc.id or "", "name": tc.function.name, "arguments": tc.function.arguments or ""}
        for tc in tool_calls
        if tc.function.name in HIDDEN_TOOL_NAMES
    ]


def _tool_results(calls: List[Dict[str, str]], results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {call["id"]: call for call in calls}
    messages: List[Dict[str, Any]] = []
    for result in results:
        call = by_id[result["id"]]
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": result["content"]})
    return messages


def _failure_feedback(calls: List[Dict[str, str]], results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {call["id"]: call for call in calls}
    feedback: List[Dict[str, Any]] = []
    for result in results:
        if result["ok"]:
            continue
        call = by_id[result["id"]]
        feedback.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": COMPRESS_TOOL_NAME, "arguments": call["arguments"]},
                    }
                ],
            }
        )
        feedback.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": "compress failed: " + "; ".join(result["errors"] or ["unknown error"]),
            }
        )
    return feedback


acp_kernel_handler = _PROXY_AcpKernelHandler.from_env()
