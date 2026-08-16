"""The security model, pinned by tests.

Every rule the README states — three gates, scoped reads, one write path,
six required sections, create-only — has a test here that fails if the code
stops enforcing it.
"""

import httpx
import pytest
from fastmcp.exceptions import ToolError

from conftest import ADMIN_TOKEN, ALICE_TOKEN, BOB_TOKEN, PATH_TOKEN, call, make_client

REPORT = (
    "## Summary\n(test)\n"
    "## User's own words\n(none)\n"
    "## Transcribed documents\n(none)\n"
    "## Advice given\n(none)\n"
    "## Self-measured values\n(none)\n"
    "## Hand-over to the local side\n(none)\n"
)


# ── Gate 1+2: the random path and the bearer token ──


def test_wrong_path_is_404(rig):
    r = httpx.post(
        rig["base_url"] + "/mcp-wrong-path",
        headers={"Authorization": f"Bearer {ALICE_TOKEN}"},
    )
    assert r.status_code == 404


def test_missing_token_is_401(rig):
    r = httpx.post(rig["mcp_url"])
    assert r.status_code == 401


def test_bad_token_is_401(rig):
    r = httpx.post(
        rig["mcp_url"], headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert r.status_code == 401


# ── Gate 3: the tool surface ──


async def test_exactly_four_tools(rig):
    async with make_client(rig, ADMIN_TOKEN) as c:
        tools = {t.name for t in await c.list_tools()}
    assert tools == {"list_dir", "read_file", "search", "save_report"}


async def test_read_own_file(rig):
    out = await call(rig, ALICE_TOKEN, "read_file", path="members/alice/history.md")
    assert "stomach ache" in out


async def test_member_prefix_autocorrect(rig):
    out = await call(rig, ALICE_TOKEN, "read_file", path="alice/history.md")
    assert "stomach ache" in out


async def test_path_traversal_is_blocked(rig):
    with pytest.raises(ToolError, match="escapes the archive"):
        await call(rig, ALICE_TOKEN, "read_file", path="../outside-secret.txt")


async def test_self_scope_cannot_read_other_member(rig):
    with pytest.raises(ToolError, match="no access"):
        await call(rig, ALICE_TOKEN, "read_file", path="members/bob/history.md")


async def test_self_scope_cannot_list_other_member(rig):
    with pytest.raises(ToolError, match="no access"):
        await call(rig, ALICE_TOKEN, "list_dir", path="members/bob")


async def test_self_scope_can_read_shared_docs(rig):
    out = await call(rig, ALICE_TOKEN, "read_file", path="docs/rules.md")
    assert "Shared rules" in out


async def test_all_scope_reads_everyone(rig):
    out = await call(rig, ADMIN_TOKEN, "read_file", path="members/bob/history.md")
    assert "hypertension" in out


async def test_binary_returns_metadata_only(rig):
    out = await call(rig, ALICE_TOKEN, "read_file", path="members/alice/scan.png")
    assert "binary" in out and "PNG" not in out


async def test_oversized_file_is_refused(rig):
    with pytest.raises(ToolError, match="exceeds"):
        await call(rig, ALICE_TOKEN, "read_file", path="members/alice/huge.md")


async def test_search_does_not_leak_other_members(rig):
    out = await call(rig, ALICE_TOKEN, "search", query="BOB-PRIVATE-MARKER")
    # the marker exists in bob's file, but for a self-scoped alice token the
    # only mention allowed back is the "no hits" echo of the query itself
    assert out.startswith("No hits") and "members/bob" not in out


async def test_search_finds_own_content(rig):
    out = await call(rig, ALICE_TOKEN, "search", query="penicillin")
    assert "allergies-medication" in out


# ── The single write path ──


async def test_save_report_lands_in_own_inbox(rig):
    out = await call(
        rig, ALICE_TOKEN, "save_report",
        date="2026-03-01", topic="follow-up", content=REPORT,
    )
    assert "members/alice/inbox/2026-03-01_follow-up.md" in out
    saved = rig["archive"] / "members/alice/inbox/2026-03-01_follow-up.md"
    assert saved.is_file()
    assert saved.read_text().startswith("---\nmember: alice\n")


async def test_save_report_never_overwrites(rig):
    for _ in range(2):
        await call(
            rig, ALICE_TOKEN, "save_report",
            date="2026-03-02", topic="same-topic", content=REPORT,
        )
    inbox = rig["archive"] / "members/alice/inbox"
    assert (inbox / "2026-03-02_same-topic.md").is_file()
    assert (inbox / "2026-03-02_same-topic-2.md").is_file()


async def test_save_report_rejects_missing_sections(rig):
    with pytest.raises(ToolError, match="missing required sections.*Self-measured values"):
        await call(
            rig, ALICE_TOKEN, "save_report",
            date="2026-03-03", topic="incomplete",
            content=REPORT.replace("## Self-measured values\n(none)\n", ""),
        )


async def test_save_report_rejects_malformed_date(rig):
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await call(
            rig, ALICE_TOKEN, "save_report",
            date="03/01/2026", topic="date", content=REPORT,
        )


async def test_save_report_rejects_impossible_date(rig):
    with pytest.raises(ToolError, match="not a real calendar date"):
        await call(
            rig, ALICE_TOKEN, "save_report",
            date="2026-13-40", topic="date", content=REPORT,
        )


async def test_save_report_cannot_target_other_member(rig):
    with pytest.raises(ToolError, match="no access"):
        await call(
            rig, ALICE_TOKEN, "save_report",
            date="2026-03-04", topic="cross-member", content=REPORT, member="bob",
        )


async def test_all_scope_falls_back_to_default_member(rig):
    out = await call(
        rig, ADMIN_TOKEN, "save_report",
        date="2026-03-05", topic="default-member", content=REPORT,
    )
    assert "members/alice/inbox" in out


async def test_topic_is_sanitized(rig):
    out = await call(
        rig, BOB_TOKEN, "save_report",
        date="2026-03-06", topic="a/b\nc d", content=REPORT,
    )
    assert "members/bob/inbox/2026-03-06_a-b_c_d.md" in out
