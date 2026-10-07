import asyncio
import itertools
import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional

SIDECAR_SCRIPT = Path(__file__).with_name("sidecar.mjs")
STREAM_LIMIT_BYTES = 256 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0


class SidecarError(RuntimeError):
    pass


class AcpKernelSidecar:
    def __init__(self, kernel_dir: str, node_bin: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.kernel_dir = kernel_dir
        self.node_bin = node_bin or os.environ.get("ACP_KERNEL_NODE") or shutil.which("node") or "node"
        self.timeout = timeout
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)

    async def call(self, op: str, **payload: Any) -> Any:
        async with self._lock:
            proc = await self._ensure_started()
            request_id = next(self._ids)
            line = json.dumps({"id": request_id, "op": op, **payload}) + "\n"
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(line.encode())
                await proc.stdin.drain()
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
            except (asyncio.TimeoutError, ConnectionError, BrokenPipeError) as exc:
                await self._kill()
                raise SidecarError(f"acp-kernel sidecar failed on {op}: {exc!r}") from exc
            if not raw:
                await self._kill()
                raise SidecarError(f"acp-kernel sidecar exited during {op}")
            reply = json.loads(raw)
            if not reply.get("ok"):
                raise SidecarError(reply.get("error", "unknown sidecar error"))
            return reply["result"]

    async def close(self) -> None:
        async with self._lock:
            await self._kill()

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        self._proc = await asyncio.create_subprocess_exec(
            self.node_bin,
            str(SIDECAR_SCRIPT),
            self.kernel_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=STREAM_LIMIT_BYTES,
        )
        return self._proc

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.kill()
        await proc.wait()
