"""Family-health MCP 服务器 — 家庭成员采集端(ChatGPT 等经开发者模式应用接入)。

职责分离(rules.md「收件箱」节):采集端只做两件事——读/搜档案以给出正确建议、
把对话生成六章节结构化报告存进本人收件箱(save_report)。
深度分析与结构化归档(记录/index/随访/病史/自测 CSV 等)由本地端独占,本服务器不提供相应工具。

多成员隔离:tokens.json 把 Bearer 令牌绑定到成员(改动后需重启服务生效)。
scope="self" 的令牌只能读写本人目录(members/<本人>/)与公共文件(docs/ 等);scope="all" 不受限。
代码层铁律:路径锁在档案库内(resolve 后校验,防穿越);收件箱只新建不覆盖;
报告六章节强校验;无删除、无重命名、无结构化档案写入;读类路径自动纠偏 <member>/→members/<member>/。

配置全部走环境变量,见 .env.example。
"""

import hmac
import json
import os
import re
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

BASE = Path(__file__).resolve().parent


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"[config] 缺少环境变量 {name}(见 .env.example)")
    return value


def _read_secret_file(path: Path, what: str) -> str:
    if not path.is_file():
        sys.exit(f"[config] 找不到{what}: {path}(见 README 的 Setup 一节)")
    content = path.read_text().strip()
    if not content:
        sys.exit(f"[config] {what}为空: {path}")
    return content


# 档案库根目录:纯文件的健康档案,本服务器只在这个范围内活动
ARCHIVE = Path(_require_env("ARCHIVE_PATH")).expanduser().resolve()
if not ARCHIVE.is_dir():
    sys.exit(f"[config] ARCHIVE_PATH 不是目录: {ARCHIVE}")

# 随机长路径(第一层门禁)与令牌表(第二层门禁)
PATH_TOKEN = _read_secret_file(
    Path(os.environ.get("PATH_TOKEN_FILE", BASE / ".path_token")).expanduser(),
    "路径令牌文件",
)
TOKENS = json.loads(
    _read_secret_file(
        Path(os.environ.get("TOKENS_FILE", BASE / "tokens.json")).expanduser(),
        "令牌表",
    )
)  # token -> {member, scope}

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
# scope="all" 的令牌未指明成员时落到谁
DEFAULT_MEMBER = os.environ.get("DEFAULT_MEMBER", "").strip()

MAX_READ_BYTES = 512 * 1024
BINARY_EXT = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp", ".pdf", ".tiff", ".bmp"}

mcp = FastMCP(
    "family-health",
    instructions=(
        "家庭健康档案库的采集端工具。①给健康建议前先 read_file 该成员的 过敏与用药.md 和 病史.md,"
        "需要背景再看 index.md/记录/自测;②save_report 的调用时机:用户说'生成报告/存档/记录一下'时"
        "无条件立即生成;对话告一段落且本轮出现新健康事实(新症状或变化/新检查结果/新图片或文件/"
        "新用药或反应/新自测数值/医生意见/明确随访决定)时主动生成,不要问'需要吗';"
        "纯解释、重复确认、无新增健康事实时不重复生成。报告格式与铁律见 save_report 工具说明。"
        "深度分析与结构化归档不归你,由本地端定期处理。"
    ),
)


def _auth() -> dict:
    """当前请求的身份(由中间件写入 request.state)。"""
    req = get_http_request()
    auth = getattr(req.state, "auth", None)
    if not auth:
        raise ValueError("未认证请求")
    return auth


def _effective_member(member: str) -> str:
    """校验调用者是否有权操作该成员;self 令牌未指明成员时自动落到本人。"""
    a = _auth()
    if a["scope"] == "all":
        resolved = member or DEFAULT_MEMBER
        if not resolved:
            raise ValueError("未指定成员,且未配置 DEFAULT_MEMBER")
        return resolved
    if member and member != a["member"]:
        raise ValueError(f"无权访问成员 {member} 的档案(你的令牌只绑定 {a['member']})")
    return a["member"]


def _resolve(rel: str) -> Path:
    p = (ARCHIVE / rel).resolve()
    if not p.is_relative_to(ARCHIVE):
        raise ValueError(f"路径越出档案库范围: {rel}")
    return p


def _user_path(rel: str) -> Path:
    """解析读类工具的用户路径,并做权限校验。
    自动纠偏:'<member>/随访.md' 这类漏写 members/ 前缀的路径,若 members/<rel> 存在则自动定位。"""
    p = _resolve(rel)
    if not p.exists():
        alt = _resolve(f"members/{rel}")
        if alt.exists():
            p = alt
    _check_read_scope(p)
    return p


def _check_read_scope(p: Path) -> None:
    """self 令牌只能读本人目录和 members 之外的公共文件(docs/ 等)。
    基于 resolve 后的真实路径校验,防 ../ 穿越绕过。"""
    a = _auth()
    if a["scope"] == "all":
        return
    parts = p.relative_to(ARCHIVE).parts
    if parts and parts[0] == "members":
        if len(parts) > 1 and parts[1] != a["member"]:
            raise ValueError(f"无权访问其他成员的档案(你的令牌只绑定 {a['member']})")


def _member_dir(member: str) -> Path:
    d = _resolve(f"members/{member}")
    if not d.is_dir():
        raise ValueError(f"成员不存在: {member}")
    return d


@mcp.tool(annotations={"readOnlyHint": True})
def list_dir(path: str = ".") -> str:
    """列出档案库内某目录的内容。path 为相对路径,默认库根目录。"""
    p = _user_path(path)
    if not p.is_dir():
        raise ValueError(f"不是目录: {path}(成员档案在 members/<名字>/ 下)")
    lines = []
    for child in sorted(p.iterdir()):
        if child.name.startswith("."):
            continue
        mark = "/" if child.is_dir() else f"  ({child.stat().st_size} B)"
        lines.append(child.name + mark)
    return "\n".join(lines) or "(空目录)"


@mcp.tool(annotations={"readOnlyHint": True})
def read_file(path: str) -> str:
    """读取档案库内一个文本文件。图片/PDF 原件只返回元数据。"""
    p = _user_path(path)
    if not p.is_file():
        raise ValueError(f"文件不存在: {path}(成员档案在 members/<名字>/ 下)")
    if p.suffix.lower() in BINARY_EXT:
        return f"[二进制原件,不支持远程读取] {path} — {p.stat().st_size} 字节。"
    if p.stat().st_size > MAX_READ_BYTES:
        raise ValueError(f"文件超过 {MAX_READ_BYTES} 字节: {path}")
    return p.read_text()


REPORT_SECTIONS = [
    "## 主题概要",
    "## 用户口述(原话)",
    "## 文件与报告转录",
    "## AI 建议要点",
    "## 自测数值",
    "## 待办与转交本地端",
]


@mcp.tool(annotations={"readOnlyHint": True})
def search(query: str, path: str = ".") -> str:
    """在档案库的文本文件(.md/.json/.csv)中全文搜索关键词(不区分大小写),
    返回 文件:行号:该行内容。适合找"上次腹痛""某项指标出现在哪"这类问题;
    找到文件后用 read_file 读全文。path 可限定搜索范围(相对路径),默认全库。"""
    q = query.strip().lower()
    if not q:
        raise ValueError("query 不能为空")
    root = _user_path(path)
    if not root.exists():
        raise ValueError(f"路径不存在: {path}(成员档案在 members/<名字>/ 下)")
    hits, scanned = [], 0
    files = [root] if root.is_file() else sorted(root.rglob("*"))
    for f in files:
        if not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in {".md", ".json", ".csv", ".txt"}:
            continue
        rel_f = f.relative_to(ARCHIVE)
        try:
            _check_read_scope(f)
        except ValueError:
            continue
        scanned += 1
        try:
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if q in line.lower():
                    hits.append(f"{rel_f}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 50:
                        return "\n".join(hits) + "\n(已达 50 条上限,建议缩小范围或换更具体的词)"
        except (UnicodeDecodeError, OSError):
            continue
    if not hits:
        return f"未找到「{query}」(扫描了 {scanned} 个文本文件)"
    return "\n".join(hits)


@mcp.tool
def save_report(date: str, topic: str, content: str, member: str = "") -> str:
    """在对话告一段落、且本轮出现新健康事实时,生成本次交流的结构化报告并存入本人收件箱,供本地医生端定期消化入库(纯解释/无新增事实不重复生成)。

    - date: 今天的日期 YYYY-MM-DD
    - topic: 简短中文主题,如 "左腹隐痛咨询"、"体检报告讨论"
    - member: 一般留空(自动落到令牌绑定的成员)
    - content: 报告正文,**必须包含以下六个二级标题章节,缺一不可**(无内容的章节写"(无)"):

      ## 主题概要
      (一段话说清本次聊了什么、结论是什么)
      ## 用户口述(原话)
      (逐条保留用户原话,不改写、不省略;时间、部位、程度等细节原样保留)
      ## 文件与报告转录
      (用户提供的文件/报告/图片的完整转录,数值、参考范围、单位一个不落;无则写(无))
      ## AI 建议要点
      (本次给出的关键建议与就医触发条件)
      ## 自测数值
      (本次提到的血压/血糖/体重等,一行一条,含日期时间;无则写(无))
      ## 待办与转交本地端
      (复查提醒、需把原件交本地端入库的文件、需本地端跟进的事项)

    写报告的铁律:
    - 用户原话逐条保留,不改写、不省略;时间、部位、程度等细节原样保留
    - 文件和图片内容完整转录,数值、单位、参考范围一个不落
    - 自测数值每条带日期和时间
    - 图片/PDF/大文件原件走独立的云盘投放口(见项目指令),本工具只收文字;仍需完整转录,并在"待办与转交本地端"注明原件的确切文件名。存云盘失败时退回仅转录并注明
    - 宁全勿简——这份报告是档案入库的唯一来源,你漏掉的信息就永远丢了
    - 存完在回复末尾用一行说明存了什么

    只新建不覆盖;同日同主题自动加序号。"""
    m = _effective_member(member)
    _member_dir(m)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date 必须是 YYYY-MM-DD")
    missing = [s for s in REPORT_SECTIONS if s not in content]
    if missing:
        raise ValueError(f"报告缺少必备章节: {'、'.join(missing)}。无内容的章节也要保留标题并写(无)")
    topic = topic.strip().replace("/", "-").replace(" ", "_") or "对话"
    rel = f"members/{m}/收件箱/{date}_{topic}.md"
    p = _resolve(rel)
    n = 2
    while p.exists():
        rel = f"members/{m}/收件箱/{date}_{topic}-{n}.md"
        p = _resolve(rel)
        n += 1
    p.parent.mkdir(parents=True, exist_ok=True)
    front = f"---\nmember: {m}\ndate: {date}\ntopic: {topic}\ntype: 对话报告\n---\n\n"
    p.write_text(front + content)
    return f"已存入 {rel},本地医生端会在定期整理时消化。"


if __name__ == "__main__":
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        """第二层门禁:静态 Bearer 令牌,常量时间比较,把身份写进 request.state。"""

        async def dispatch(self, request, call_next):
            supplied = request.headers.get("authorization", "")
            for token, info in TOKENS.items():
                if hmac.compare_digest(supplied, f"Bearer {token}"):
                    request.state.auth = info
                    return await call_next(request)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    app = mcp.http_app(path=f"/mcp-{PATH_TOKEN}")
    app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(app, host=HOST, port=PORT)
