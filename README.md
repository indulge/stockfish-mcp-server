# Stockfish MCP Server

A local [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that
exposes the [Stockfish](https://stockfishchess.org/) chess engine to any MCP client
(Claude Desktop, Claude Code, Cline, Continue, your own client, …).

It speaks MCP over **stdio**. All tools are **stateless** — every call takes a FEN
string, so the server holds no game state.

---

## Tools

| Tool | Purpose |
|------|---------|
| `analyze_position` | Evaluate a position; returns score (White POV), best move, and principal variation(s). Supports `multipv`, `depth`, or `movetime_ms`. |
| `get_best_move` | Pick a move for the side to move. Optional `skill_level` (0–20) or `elo` (~1320–3190) to play at reduced strength. |
| `evaluate_position` | Lighter/faster single-line evaluation. |
| `apply_moves` | Play a sequence of SAN/UCI moves and return the resulting FEN. |
| `get_legal_moves` | List all legal moves (SAN + UCI). |
| `visualize_board` | Render a position as a Unicode/ASCII board. |
| `engine_info` | Engine name/version and default options. |

Every tool defaults to the standard starting position when `fen` is omitted.
Scores are from White's point of view; mate is reported as `#±N`.

---

## Dependencies

| Dependency | Version | Why |
|---|---|---|
| **Python** | ≥ 3.14 | Runtime (see `.python-version`). |
| **[python-chess](https://python-chess.readthedocs.io/)** (`chess`) | ≥ 1.11.2 | Board logic + UCI engine driver. |
| **[FastMCP](https://gofastmcp.com/)** (`fastmcp`) | ≥ 3.4.2 | MCP server framework. |
| **Stockfish binary** | 17/18+ | The engine itself. **Not bundled** (it is >100 MB); download it — see below. |

Python deps are declared in `pyproject.toml` and pinned in `uv.lock`.
[`uv`](https://docs.astral.sh/uv/) is the recommended installer/runner, but plain
`pip`/`venv` works too.

---

## Install

```bash
# 1. Get the code
git clone <this-repo-url>
cd stockfish-mcp-server

# 2. Get the Stockfish engine binary (NOT in git — too large).
#    This downloads it to engine/stockfish:
./engine/download-stockfish.sh
#    ...or set SF_ASSET to match your CPU/OS, e.g.:
#    SF_ASSET=stockfish-ubuntu-x86-64-avx512 ./engine/download-stockfish.sh
#    Browse builds: https://github.com/official-stockfish/Stockfish/releases/latest
#    Already have Stockfish? Skip this and set STOCKFISH_PATH (see Configuration).

# 3a. Install dependencies with uv (recommended)
uv sync

# 3b. ...or with pip
python -m venv .venv && . .venv/bin/activate
pip install "chess>=1.11.2" "fastmcp>=3.4.2"
```

### Optional: install as a command

The project is packaged with a `stockfish-mcp` console-script entry point:

```bash
uv tool install .        # puts `stockfish-mcp` on your PATH (~/.local/bin)
# or: pip install .
```

---

## Run

```bash
uv run python server.py     # run the stdio server (uv)
# or, if installed as a command:
stockfish-mcp
```

The server communicates over stdio and is normally launched **by your MCP client**
(below), not by hand. Stop it with Ctrl-C / SIGTERM.

---

## Use it with an MCP client

MCP stdio clients launch the server as a subprocess. Point your client at either
`uv run python /abs/path/to/server.py` or the installed `stockfish-mcp` command,
and set `STOCKFISH_PATH` if the binary isn't at `engine/stockfish`.

A ready-to-edit example is in [`examples/mcp-config.json`](examples/mcp-config.json).

**Generic MCP config** (the shape most clients accept — e.g. a `mcpServers` map in
Claude Desktop's `claude_desktop_config.json`, or `.mcp.json`):

```json
{
  "mcpServers": {
    "stockfish": {
      "command": "uv",
      "args": ["run", "python", "/abs/path/to/stockfish-mcp-server/server.py"],
      "env": {
        "STOCKFISH_PATH": "/abs/path/to/stockfish-mcp-server/engine/stockfish"
      }
    }
  }
}
```

If you installed the command (`uv tool install .`), use it directly:

```json
{
  "mcpServers": {
    "stockfish": {
      "command": "stockfish-mcp",
      "env": { "STOCKFISH_PATH": "/abs/path/to/engine/stockfish" }
    }
  }
}
```

**Claude Code** (CLI) — register in user scope so it's available everywhere:

```bash
claude mcp add stockfish -s user \
  -e STOCKFISH_PATH=/abs/path/to/engine/stockfish \
  -- stockfish-mcp
claude mcp list      # should show: stockfish … ✔ Connected
```

Then ask the model things like *"analyze this FEN at depth 20"* or
*"play a move against me at 1500 Elo."*

> Tip: launching the executable directly (the installed `stockfish-mcp`, or the
> venv's `python`) rather than via a wrapper makes the server the client's direct
> child process, so its shutdown signal handling works cleanly.

---

## Configuration (environment variables)

| Variable | Default | Meaning |
|----------|---------|---------|
| `STOCKFISH_PATH` | `engine/stockfish` (next to `server.py`) | Path to the Stockfish binary. |
| `STOCKFISH_THREADS` | `min(4, ncpu)` | Search threads. |
| `STOCKFISH_HASH_MB` | `256` | Transposition-table size (MB). |

Per-call search cost is capped (depth ≤ 30, movetime ≤ 60 s, multipv ≤ 10) so a
single request can't stall the stdio loop.

---

## Test

```bash
uv run python test_server.py    # end-to-end smoke test via an in-memory MCP client
```

It lists the tools and exercises analysis, mate detection, reduced-strength play,
move application, and error handling — no external MCP client needed.

---

## Files

```
server.py                 The MCP server (FastMCP + python-chess)
test_server.py            End-to-end smoke test
pyproject.toml            Package metadata + dependencies (entry point: stockfish-mcp)
uv.lock                   Pinned dependency versions
.python-version           Python version (3.14)
engine/
  download-stockfish.sh   Fetches the engine binary (binary itself is gitignored)
  LICENSE-stockfish.txt   Stockfish license (GPLv3)
```

---

## References

- Model Context Protocol — https://modelcontextprotocol.io
- FastMCP — https://gofastmcp.com/
- python-chess — https://python-chess.readthedocs.io/
- Stockfish — https://stockfishchess.org/ · releases: https://github.com/official-stockfish/Stockfish/releases/latest
- uv — https://docs.astral.sh/uv/

---

## License

This server's code is provided as-is. **Stockfish is licensed under GPLv3** — see
`engine/LICENSE-stockfish.txt`. If you redistribute the Stockfish binary, you must
comply with the GPLv3 (including providing source).
