"""Test rig: a real HTTP server over a throwaway archive.

The suite talks to the server the same way ChatGPT does — streamable HTTP,
random path segment, bearer token — so the middleware and every scope check
are exercised, not mocked. Config is read at import time, so the environment
is prepared here before `server` is imported.
"""

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

PATH_TOKEN = "test-path-token"
ALICE_TOKEN = "alice-self-token"
BOB_TOKEN = "bob-self-token"
ADMIN_TOKEN = "admin-all-token"


def _build_archive(root: Path) -> None:
    alice = root / "members" / "alice"
    bob = root / "members" / "bob"
    (alice / "收件箱").mkdir(parents=True)
    (bob / "收件箱").mkdir(parents=True)
    (root / "docs").mkdir()

    (root / "docs" / "rules.md").write_text("# 公共规则\n所有成员可读。\n")
    (alice / "病史.md").write_text("# 病史\n2026-01-02 腹痛就诊,已缓解。\n")
    (alice / "过敏与用药.md").write_text("# 过敏与用药\n青霉素过敏。\n")
    (bob / "病史.md").write_text("# 病史\nBOB-PRIVATE-MARKER 高血压随访中。\n")
    (alice / "scan.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    (alice / "huge.md").write_text("x" * (512 * 1024 + 1))


@pytest.fixture(scope="session")
def rig(tmp_path_factory):
    archive = tmp_path_factory.mktemp("archive")
    secrets = tmp_path_factory.mktemp("secrets")
    _build_archive(archive)

    # A file *outside* the archive that traversal must never reach
    outside = archive.parent / "outside-secret.txt"
    outside.write_text("OUTSIDE-THE-ARCHIVE\n")

    (secrets / ".path_token").write_text(PATH_TOKEN + "\n")
    (secrets / "tokens.json").write_text(
        f'{{"{ALICE_TOKEN}": {{"member": "alice", "scope": "self"}},'
        f' "{BOB_TOKEN}": {{"member": "bob", "scope": "self"}},'
        f' "{ADMIN_TOKEN}": {{"member": "", "scope": "all"}}}}'
    )

    os.environ["ARCHIVE_PATH"] = str(archive)
    os.environ["PATH_TOKEN_FILE"] = str(secrets / ".path_token")
    os.environ["TOKENS_FILE"] = str(secrets / "tokens.json")
    os.environ["DEFAULT_MEMBER"] = "alice"

    import server  # config is read at import — env must already be set

    config = uvicorn.Config(
        server.build_app(), host="127.0.0.1", port=0, log_level="warning"
    )
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    port = srv.servers[0].sockets[0].getsockname()[1]

    yield {
        "base_url": f"http://127.0.0.1:{port}",
        "mcp_url": f"http://127.0.0.1:{port}/mcp-{PATH_TOKEN}",
        "archive": archive,
    }

    srv.should_exit = True
    thread.join(timeout=5)


def make_client(rig, token: str) -> Client:
    return Client(
        StreamableHttpTransport(
            rig["mcp_url"], headers={"Authorization": f"Bearer {token}"}
        )
    )


async def call(rig, token: str, tool: str, **args) -> str:
    async with make_client(rig, token) as c:
        result = await c.call_tool(tool, args)
        return result.content[0].text
