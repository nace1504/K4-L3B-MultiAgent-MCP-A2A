from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


def _dump(case_id: str, record: dict[str, Any]) -> None:
    dump_dir = os.environ.get("DAY09_DUMP_DIR")
    if not dump_dir:
        return
    dump_path = Path(dump_dir) / f"{case_id}.jsonl"
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    with dump_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._cache: dict[tuple, object] = {}

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        cache_key = (tool_name, case_id, tuple(sorted(arguments.items())))
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if isinstance(cached, BaseException):
                raise cached
            return cached

        payload = {"case_id": case_id, **arguments}
        try:
            try:
                result = await self._session.call_tool(tool_name, arguments=payload)
            except (httpx2.TimeoutException, httpx2.TransportError):
                result = await self._session.call_tool(tool_name, arguments=payload)

            is_error = getattr(result, "is_error", getattr(result, "isError", False))
            if is_error:
                # the gateway fails transiently (seen on the first calls of a run):
                # one delayed retry, the tool calls are read-only so this is idempotent
                await asyncio.sleep(1.5)
                result = await self._session.call_tool(tool_name, arguments=payload)
                is_error = getattr(result, "is_error", getattr(result, "isError", False))
            if is_error:
                message = " ".join(
                    block.text for block in result.content if getattr(block, "text", None)
                )
                raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")

            evidence = getattr(result, "structured_content", None)
            if evidence is None:
                evidence = getattr(result, "structuredContent", None)
            if evidence is None:
                text_blocks = [
                    block.text for block in result.content if getattr(block, "text", None)
                ]
                if len(text_blocks) != 1:
                    raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
                evidence = json.loads(text_blocks[0])

            self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
            self._cache[cache_key] = evidence
            _dump(
                case_id,
                {"tool": tool_name, "args": payload, "ok": True, "evidence": evidence},
            )
            return evidence
        except Exception as exc:
            self._cache[cache_key] = exc
            _dump(
                case_id,
                {"tool": tool_name, "args": payload, "ok": False, "error": str(exc)},
            )
            raise


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
