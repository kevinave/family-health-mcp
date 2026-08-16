"""Family-health MCP server — the collector side, for a hosted LLM client.

Separation of duties: the collector does two things — read and search the
archive so advice is grounded, and deposit one structured six-section report
per conversation into the member's inbox (save_report). Deep analysis and
structured archiving (history, index, follow-ups, measurement series) belong
to the local side, so this server deliberately has no tools for them.

Member isolation: tokens.json binds each bearer token to a member (restart to
apply changes). A scope="self" token reaches its own directory
(members/<name>/) and shared files (docs/ etc.); scope="all" is unrestricted.

Enforced in code, not requested in the prompt: paths are locked inside the
archive (resolved first, then checked, so ../ cannot escape); the inbox is
create-only; the six report sections are validated; no delete, no rename, no
writes to the structured record; read paths get members/<member>/
autocorrection.

All configuration is environment variables — see .env.example.
"""

import hmac
import json
import os
import re
import sys
from datetime import date as _date
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

BASE = Path(__file__).resolve().parent


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"[config] missing environment variable {name} (see .env.example)")
    return value


def _read_secret_file(path: Path, what: str) -> str:
    if not path.is_file():
        sys.exit(f"[config] {what} not found: {path} (see the Setup section of the README)")
    content = path.read_text().strip()
    if not content:
        sys.exit(f"[config] {what} is empty: {path}")
    return content


# Root of the file-based health archive; the server never acts outside it.
ARCHIVE = Path(_require_env("ARCHIVE_PATH")).expanduser().resolve()
if not ARCHIVE.is_dir():
    sys.exit(f"[config] ARCHIVE_PATH is not a directory: {ARCHIVE}")

# The long random path (gate one) and the token table (gate two).
PATH_TOKEN = _read_secret_file(
    Path(os.environ.get("PATH_TOKEN_FILE", BASE / ".path_token")).expanduser(),
    "path token file",
)
TOKENS = json.loads(
    _read_secret_file(
        Path(os.environ.get("TOKENS_FILE", BASE / "tokens.json")).expanduser(),
        "token table",
    )
)  # token -> {member, scope}

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
# Which member a scope="all" token falls back to when it names no one.
DEFAULT_MEMBER = os.environ.get("DEFAULT_MEMBER", "").strip()
# Directory name of the single write path inside each member's folder. A
# localized archive names it in its own language — the reference deployment
# runs a Chinese archive and sets 收件箱.
INBOX_DIRNAME = os.environ.get("INBOX_DIRNAME", "inbox").strip() or "inbox"

MAX_READ_BYTES = 512 * 1024
BINARY_EXT = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp", ".pdf", ".tiff", ".bmp"}

mcp = FastMCP(
    "family-health",
    instructions=(
        "Collector-side tools for a family health archive. "
        "① Before giving health advice, read_file the member's "
        "allergies-medication.md and history.md; pull index.md, notes/ and "
        "measurements/ when more background is needed. "
        "② When to save_report: the moment the user says 'save this / make a "
        "report / put that on file', unconditionally and immediately; and "
        "proactively when a conversation segment closes carrying new health "
        "facts (a new or changed symptom, a new result, a new image or "
        "document, a new medication or reaction, a new self-measured value, a "
        "doctor's opinion, an explicit follow-up decision) — do not ask "
        "'shall I'. Pure explanation, repeated confirmation, or chat with "
        "nothing new does not need another report. Format and hard rules are "
        "in save_report's description. Deep analysis and structured archiving "
        "are not yours; the local side does them on its own schedule."
    ),
)


def _auth() -> dict:
    """Identity of the current request (written by the middleware)."""
    req = get_http_request()
    auth = getattr(req.state, "auth", None)
    if not auth:
        raise ValueError("unauthenticated request")
    return auth


def _effective_member(member: str) -> str:
    """Check the caller may act for this member; a self token naming no one falls back to itself."""
    a = _auth()
    if a["scope"] == "all":
        resolved = member or DEFAULT_MEMBER
        if not resolved:
            raise ValueError("no member given, and DEFAULT_MEMBER is not configured")
        return resolved
    if member and member != a["member"]:
        raise ValueError(f"no access to member {member}'s files (your token is bound to {a['member']})")
    return a["member"]


def _resolve(rel: str) -> Path:
    p = (ARCHIVE / rel).resolve()
    if not p.is_relative_to(ARCHIVE):
        raise ValueError(f"path escapes the archive: {rel}")
    return p


def _user_path(rel: str) -> Path:
    """Resolve a user-supplied read path and check the scope.
    Autocorrection: for a path like '<member>/history.md' that forgot the
    members/ prefix, use members/<rel> when that exists."""
    p = _resolve(rel)
    if not p.exists():
        alt = _resolve(f"members/{rel}")
        if alt.exists():
            p = alt
    _check_read_scope(p)
    return p


def _check_read_scope(p: Path) -> None:
    """A self token reads its own directory and the shared files outside
    members/ (docs/ etc.). Checked on the *resolved* path, so ../ cannot
    route around it."""
    a = _auth()
    if a["scope"] == "all":
        return
    parts = p.relative_to(ARCHIVE).parts
    if parts and parts[0] == "members":
        if len(parts) > 1 and parts[1] != a["member"]:
            raise ValueError(f"no access to another member's files (your token is bound to {a['member']})")


def _member_dir(member: str) -> Path:
    d = _resolve(f"members/{member}")
    if not d.is_dir():
        raise ValueError(f"member does not exist: {member}")
    return d


@mcp.tool(annotations={"readOnlyHint": True})
def list_dir(path: str = ".") -> str:
    """List a directory inside the archive. path is relative; default is the archive root."""
    p = _user_path(path)
    if not p.is_dir():
        raise ValueError(f"not a directory: {path} (member files live under members/<name>/)")
    lines = []
    for child in sorted(p.iterdir()):
        if child.name.startswith("."):
            continue
        mark = "/" if child.is_dir() else f"  ({child.stat().st_size} B)"
        lines.append(child.name + mark)
    return "\n".join(lines) or "(empty directory)"


@mcp.tool(annotations={"readOnlyHint": True})
def read_file(path: str) -> str:
    """Read one text file inside the archive. Image/PDF originals return metadata only."""
    p = _user_path(path)
    if not p.is_file():
        raise ValueError(f"no such file: {path} (member files live under members/<name>/)")
    if p.suffix.lower() in BINARY_EXT:
        return f"[binary original, not readable remotely] {path} — {p.stat().st_size} bytes."
    if p.stat().st_size > MAX_READ_BYTES:
        raise ValueError(f"file exceeds {MAX_READ_BYTES} bytes: {path}")
    return p.read_text()


REPORT_SECTIONS = [
    "## Summary",
    "## User's own words",
    "## Transcribed documents",
    "## Advice given",
    "## Self-measured values",
    "## Hand-over to the local side",
]


@mcp.tool(annotations={"readOnlyHint": True})
def search(query: str, path: str = ".") -> str:
    """Full-text search (case-insensitive) across the archive's text files
    (.md/.json/.csv), returning file:line:content. Made for questions like
    "when did the stomach ache last come up" or "where does this value
    appear"; read_file the hits for full context. path narrows the scope
    (relative); default is the whole archive."""
    q = query.strip().lower()
    if not q:
        raise ValueError("query must not be empty")
    root = _user_path(path)
    if not root.exists():
        raise ValueError(f"no such path: {path} (member files live under members/<name>/)")
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
                        return "\n".join(hits) + "\n(50-match cap reached — narrow the path or use a more specific query)"
        except (UnicodeDecodeError, OSError):
            continue
    if not hits:
        return f'No hits for "{query}" (scanned {scanned} text files)'
    return "\n".join(hits)


@mcp.tool
def save_report(date: str, topic: str, content: str, member: str = "") -> str:
    """When a conversation segment closes carrying new health facts, write the structured report of this exchange into the member's own inbox, for the local side to file during its periodic review (pure explanation with nothing new does not need a report).

    - date: today's date, YYYY-MM-DD
    - topic: a short topic, e.g. "left-side abdominal pain", "lab results discussion"
    - member: normally left empty (falls back to the member bound to the token)
    - content: the report body, which **must contain all six of these second-level headings** (write "(none)" under any that are empty):

      ## Summary
      (one paragraph: what was discussed, and what was concluded)
      ## User's own words
      (verbatim quotes, one per line, in the user's original language — no paraphrase, no omission; times, locations, intensities kept exactly)
      ## Transcribed documents
      (full transcription of any file/report/image shown in the conversation — every value, unit and reference range; "(none)" if nothing was shown)
      ## Advice given
      (the key advice from this exchange, and the thresholds that should trigger a doctor's visit)
      ## Self-measured values
      (blood pressure / glucose / weight etc. mentioned this time, one per line, each with date and time; "(none)" if none)
      ## Hand-over to the local side
      (follow-up reminders, originals the user must drop into the archive, anything the local side should pick up)

    Hard rules:
    - The user's words are kept verbatim in their original language — never paraphrased, never trimmed; times, locations and intensities stay exactly as said.
    - Documents and images are transcribed completely: values, units, reference ranges, every one of them.
    - Every self-measured value carries its date and time.
    - This tool takes text only. Images and PDFs shown in the conversation are transcribed in full into "Transcribed documents", and "Hand-over to the local side" reminds the user to drop the originals into the archive themselves (the originals are filed by the user, not by this tool).
    - Too much beats too little — this report is the archive's only source for this conversation; whatever it misses is lost.
    - End the reply with one line saying what was saved.

    Create-only, never overwrites; a same-day, same-topic report gets a numeric suffix."""
    m = _effective_member(member)
    _member_dir(m)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        _date.fromisoformat(date)
    except ValueError:
        raise ValueError(f"date is not a real calendar date: {date}")
    missing = [s for s in REPORT_SECTIONS if s not in content]
    if missing:
        raise ValueError(f"report is missing required sections: {', '.join(missing)}. Keep every heading; write (none) under the empty ones")
    # Sanitize the topic: slashes become hyphens; all whitespace (newlines
    # included, to keep front matter uninjectable) collapses to underscores.
    topic = re.sub(r"[/\\]", "-", topic)
    topic = re.sub(r"\s+", "_", topic.strip()) or "conversation"
    rel = f"members/{m}/{INBOX_DIRNAME}/{date}_{topic}.md"
    p = _resolve(rel)
    n = 2
    while p.exists():
        rel = f"members/{m}/{INBOX_DIRNAME}/{date}_{topic}-{n}.md"
        p = _resolve(rel)
        n += 1
    p.parent.mkdir(parents=True, exist_ok=True)
    front = f"---\nmember: {m}\ndate: {date}\ntopic: {topic}\ntype: conversation-report\n---\n\n"
    p.write_text(front + content)
    return f"Saved to {rel}; the local side files it during its periodic review."


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Gate two: static bearer tokens, constant-time comparison, identity written onto request.state."""

    async def dispatch(self, request, call_next):
        supplied = request.headers.get("authorization", "")
        for token, info in TOKENS.items():
            if hmac.compare_digest(supplied, f"Bearer {token}"):
                request.state.auth = info
                return await call_next(request)
        return JSONResponse({"error": "unauthorized"}, status_code=401)


def build_app():
    """Assemble the full ASGI app (random path + bearer middleware). Tests and __main__ share this assembly path."""
    app = mcp.http_app(path=f"/mcp-{PATH_TOKEN}")
    app.add_middleware(BearerAuthMiddleware)
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_app(), host=HOST, port=PORT)
