# family-health-mcp

An MCP server that lets a hosted LLM client read a **local, file-based health archive** and write structured summaries back into it — without ever giving the model write access to the archive itself.

Running continuously since July 2026 as a personal system.

> The code comments and tool descriptions are in Chinese: they are not documentation, they are the prompt the model actually reads at runtime, and the archive they describe is Chinese. This README is the English documentation.

---

## The problem

A health archive is worth keeping in plain files — reports, medication lists, symptom notes, lab values — because plain files outlive every app you would otherwise store them in. But a plain-file archive is only useful if something can read it back to you when you need it, including when you are away from the machine that holds it.

A hosted assistant is good at that conversation. It is a bad choice for *owning* the archive: it has no memory across sessions, it will happily reorganise files you did not ask it to touch, and giving it general write access to medical records means a bad turn can quietly corrupt the record.

So the split this project implements is:

**the hosted model collects, the local side archives.**

The remote end can read anything it is allowed to see and can deposit one thing — a structured report — into an inbox. Everything that changes the shape of the archive happens locally, on demand, under review.

---

## Architecture

```
   ChatGPT (developer-mode MCP client)
             │
             │  HTTPS · random path · Bearer token
             ▼
   ┌───────────────────────┐
   │   Cloudflare Tunnel   │        no inbound ports on the host
   └───────────┬───────────┘
               ▼
   ┌───────────────────────────────────────┐
   │  this server        127.0.0.1:8787    │
   │                                       │
   │  BearerAuthMiddleware  ── identity    │
   │  path/scope guards     ── authority   │
   │  four tools            ── capability  │
   └───────────────┬───────────────────────┘
                   ▼
        ~/health-archive/           ← plain files, single source of truth
          docs/                        shared rules and procedures
          members/<name>/
            过敏与用药.md               allergies and medication
            病史.md                     history
            index.md                    timeline
            记录/ 自测/ 原件/            notes, self-measurements, originals
            收件箱/          ← the ONLY path this server can write to
                   ▲
                   │  reviewed and filed later, locally
                   └──────  local agent (full read/write)
```

---

## Design decisions

### 1. The output contract is enforced by the server, not requested in the prompt

`save_report` rejects any report that does not contain all six required sections. Not "please include these sections" in an instruction the model may drift away from — a validation that fails the tool call:

```python
missing = [s for s in REPORT_SECTIONS if s not in content]
if missing:
    raise ValueError(f"报告缺少必备章节: ...")
```

The six sections are: topic summary · the user's own words, verbatim · full transcription of any file or report · advice given · self-measured values · handover items for the local side. The one that matters most is the second: the model is required to preserve what the user actually said, not its paraphrase, because the paraphrase is where detail silently disappears.

### 2. Least privilege, expressed as capability rather than instruction

| tool | access | what it can do |
|---|---|---|
| `list_dir` | read-only | list a directory inside the archive |
| `read_file` | read-only | read one text file; binaries return metadata only |
| `search` | read-only | full-text search across `.md` / `.json` / `.csv` / `.txt` |
| `save_report` | write | create **one new file** in the caller's own inbox |

There is no delete, no rename, no move, and no tool that writes to the structured archive — history, medication lists, timelines and measurement series are all unreachable from the remote end. `save_report` never overwrites: a same-day, same-topic report gets a numeric suffix instead.

The remote model once proposed five additional tools for itself. All five were declined, because each one moved a decision from the reviewed local side to the unreviewed remote side. The tool surface is the security boundary, so it is kept small on purpose.

### 3. Multi-tenant scoping checked on resolved paths

Each bearer token maps to `{member, scope}`. A `self` token can only reach its own member directory plus shared documents; `all` is unrestricted. The check runs on the **resolved** path, so `../` traversal cannot walk out of a member directory or out of the archive:

```python
p = (ARCHIVE / rel).resolve()
if not p.is_relative_to(ARCHIVE):
    raise ValueError("路径越出档案库范围")
```

Adding a family member is: create their directory, add a token line, restart. Revoking is: delete the line, restart.

### 4. Predictable model mistakes get fixed in code, not in the prompt

The remote model has no memory across sessions, so every new conversation repeats the same mistakes. Asking it more nicely does not help — it has never seen the previous conversation. Repeated, predictable friction belongs in the server.

Concretely: the model kept writing `alice/history.md` instead of `members/alice/history.md`. Rather than adding another line to the prompt, read paths now self-correct — if `<path>` does not exist but `members/<path>` does, the latter is used.

---

## Security model

Three independent layers, each of which must pass:

1. **A long random path.** The MCP endpoint is mounted at `/mcp-<path_token>`; the URL alone is unguessable and is not published.
2. **A bearer token**, compared with `hmac.compare_digest`, resolving to an identity that is attached to the request.
3. **A small, read-biased tool surface**, plus the resolved-path scope checks above.

The host exposes no inbound ports; the tunnel makes an outbound connection. Path token and bearer token are separate files so either can be rotated on its own.

**Known limitation.** Static bearer tokens are not part of the MCP authorization spec, which expects OAuth. This works because the client accepts a static access token. If that ever changes, this is the piece that has to be replaced.

---

## Setup

```bash
git clone https://github.com/kevinave/family-health-mcp.git
cd family-health-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                     # then set ARCHIVE_PATH
cp tokens.example.json tokens.json       # then generate real tokens
python3 -c "import secrets; print(secrets.token_hex(24))" > .path_token

set -a; source .env; set +a
python3 server.py
```

Generate one token per person:

```bash
python3 -c "import secrets; print(secrets.token_hex(24))"
```

Put it in `tokens.json` as `"<token>": {"member": "alice", "scope": "self"}`.

The archive is expected to look like the tree in the diagram above; at minimum each member needs `members/<name>/` to exist before `save_report` will accept anything for them.

### Connecting the client

Expose `127.0.0.1:8787` over a tunnel (this deployment uses a named Cloudflare tunnel) and add the resulting URL as a developer-mode MCP connector:

- URL — `https://<your-host>/mcp-<path_token>`
- Auth — access token / API key, bearer scheme, using the token from `tokens.json`

`deploy/` contains example launchd and cloudflared configuration for running the server and the tunnel as always-on services.

---

## Notes from operation

Two failures worth writing down, because in both cases the obvious diagnosis was wrong.

**`read_file` failed with `[Errno 11] Resource deadlock avoided` while `list_dir` and `search` looked fine.** The archive lives in a cloud-synced folder; under disk pressure the OS had evicted files to dataless placeholders. Reading one synchronously inside the server's single-threaded event loop triggered an in-process materialisation deadlock. What made it look like a single broken tool was `search`'s own error handling — its `except (UnicodeDecodeError, OSError): continue` swallowed exactly the same error, so only `read_file` surfaced it. The fix was at the storage layer (pin the archive to local storage), not a "force download before reading" patch in the server: the server was not the thing that was wrong.

**After renaming a tool, the client reported `Unknown tool` — but the tools that had *not* been renamed kept working.** Client-side tool lists are cached and are not refreshed automatically. Any change to the tool set now ends with: refresh the connector, then start a new conversation.

---

## Scope

This is a personal system published as a reference implementation, not a product. It assumes a single trusted operator, a small number of users, and an archive that fits on one machine.

**It is not medical software and gives no medical advice.** The assistant's role in this design is to record what was said and to surface what is already in the archive. Diagnosis is not one of its tools.

---

## License

MIT — see [LICENSE](LICENSE).
