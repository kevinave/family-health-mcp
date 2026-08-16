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
    "## 主题概要\n(测试)\n"
    "## 用户口述(原话)\n(无)\n"
    "## 文件与报告转录\n(无)\n"
    "## AI 建议要点\n(无)\n"
    "## 自测数值\n(无)\n"
    "## 待办与转交本地端\n(无)\n"
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
    out = await call(rig, ALICE_TOKEN, "read_file", path="members/alice/病史.md")
    assert "腹痛" in out


async def test_member_prefix_autocorrect(rig):
    out = await call(rig, ALICE_TOKEN, "read_file", path="alice/病史.md")
    assert "腹痛" in out


async def test_path_traversal_is_blocked(rig):
    with pytest.raises(ToolError, match="越出档案库"):
        await call(rig, ALICE_TOKEN, "read_file", path="../outside-secret.txt")


async def test_self_scope_cannot_read_other_member(rig):
    with pytest.raises(ToolError, match="无权访问"):
        await call(rig, ALICE_TOKEN, "read_file", path="members/bob/病史.md")


async def test_self_scope_cannot_list_other_member(rig):
    with pytest.raises(ToolError, match="无权访问"):
        await call(rig, ALICE_TOKEN, "list_dir", path="members/bob")


async def test_self_scope_can_read_shared_docs(rig):
    out = await call(rig, ALICE_TOKEN, "read_file", path="docs/rules.md")
    assert "公共规则" in out


async def test_all_scope_reads_everyone(rig):
    out = await call(rig, ADMIN_TOKEN, "read_file", path="members/bob/病史.md")
    assert "高血压" in out


async def test_binary_returns_metadata_only(rig):
    out = await call(rig, ALICE_TOKEN, "read_file", path="members/alice/scan.png")
    assert "二进制" in out and "PNG" not in out


async def test_oversized_file_is_refused(rig):
    with pytest.raises(ToolError, match="超过"):
        await call(rig, ALICE_TOKEN, "read_file", path="members/alice/huge.md")


async def test_search_does_not_leak_other_members(rig):
    out = await call(rig, ALICE_TOKEN, "search", query="BOB-PRIVATE-MARKER")
    # the marker exists in bob's file, but for a self-scoped alice token the
    # only mention allowed back is the "not found" echo of the query itself
    assert out.startswith("未找到") and "members/bob" not in out


async def test_search_finds_own_content(rig):
    out = await call(rig, ALICE_TOKEN, "search", query="青霉素")
    assert "过敏与用药" in out


# ── The single write path ──


async def test_save_report_lands_in_own_inbox(rig):
    out = await call(
        rig, ALICE_TOKEN, "save_report",
        date="2026-03-01", topic="腹痛复诊", content=REPORT,
    )
    assert "members/alice/收件箱/2026-03-01_腹痛复诊.md" in out
    saved = rig["archive"] / "members/alice/收件箱/2026-03-01_腹痛复诊.md"
    assert saved.is_file()
    assert saved.read_text().startswith("---\nmember: alice\n")


async def test_save_report_never_overwrites(rig):
    for _ in range(2):
        await call(
            rig, ALICE_TOKEN, "save_report",
            date="2026-03-02", topic="同名", content=REPORT,
        )
    inbox = rig["archive"] / "members/alice/收件箱"
    assert (inbox / "2026-03-02_同名.md").is_file()
    assert (inbox / "2026-03-02_同名-2.md").is_file()


async def test_save_report_rejects_missing_sections(rig):
    with pytest.raises(ToolError, match="缺少必备章节.*自测数值"):
        await call(
            rig, ALICE_TOKEN, "save_report",
            date="2026-03-03", topic="残缺",
            content=REPORT.replace("## 自测数值\n(无)\n", ""),
        )


async def test_save_report_rejects_malformed_date(rig):
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await call(
            rig, ALICE_TOKEN, "save_report",
            date="03/01/2026", topic="日期", content=REPORT,
        )


async def test_save_report_rejects_impossible_date(rig):
    with pytest.raises(ToolError, match="真实存在"):
        await call(
            rig, ALICE_TOKEN, "save_report",
            date="2026-13-40", topic="日期", content=REPORT,
        )


async def test_save_report_cannot_target_other_member(rig):
    with pytest.raises(ToolError, match="无权访问"):
        await call(
            rig, ALICE_TOKEN, "save_report",
            date="2026-03-04", topic="越权", content=REPORT, member="bob",
        )


async def test_all_scope_falls_back_to_default_member(rig):
    out = await call(
        rig, ADMIN_TOKEN, "save_report",
        date="2026-03-05", topic="默认成员", content=REPORT,
    )
    assert "members/alice/收件箱" in out


async def test_topic_is_sanitized(rig):
    out = await call(
        rig, BOB_TOKEN, "save_report",
        date="2026-03-06", topic="a/b\nc d", content=REPORT,
    )
    assert "members/bob/收件箱/2026-03-06_a-b_c_d.md" in out
