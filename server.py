"""Stockfish MCP server.

Exposes a locally-installed Stockfish chess engine to Claude over MCP (stdio).
Tools are stateless: every call takes a FEN, so the server holds no game state.

Engine lifecycle: a single Stockfish process is started lazily and reused across
calls. python-chess's SimpleEngine is NOT safe for concurrent use (it runs a
background reader thread), so every engine operation is serialized with one lock.
FastMCP runs tool functions in a worker thread, so blocking here is fine.
"""

from __future__ import annotations

import atexit
import os
import signal
import threading
from pathlib import Path

import chess
import chess.engine
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

mcp = FastMCP(
    name="stockfish",
    instructions=(
        "Analyze chess positions with the Stockfish engine. All tools are "
        "stateless and take a FEN string (default: the standard starting "
        "position). Use `analyze_position` for evaluation and best lines, "
        "`get_best_move` to pick a move (optionally at reduced strength), "
        "`apply_moves` to play moves and get the resulting FEN, and "
        "`get_legal_moves` / `visualize_board` for board inspection."
    ),
)

# --- Cost caps so a single call can't stall the stdio loop -------------------
MAX_DEPTH = 30
MAX_MOVETIME_MS = 60_000
MAX_MULTIPV = 10
MATE_SCORE = 100_000  # centipawn value used when forcing a mate score to an int

# --- Engine management -------------------------------------------------------
_engine: chess.engine.SimpleEngine | None = None
_lock = threading.Lock()


def _engine_path() -> str:
    """Resolve the Stockfish binary independent of the process cwd."""
    override = os.environ.get("STOCKFISH_PATH")
    if override:
        return override
    return str(Path(__file__).resolve().parent / "engine" / "stockfish")


# Load libc once at import (single-threaded) so the post-fork preexec_fn below
# never has to dlopen — calling CDLL after fork in a multithreaded process can
# deadlock on the loader lock (preexec_fn must be async-signal-safe).
try:
    import ctypes

    _LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
except Exception:
    _LIBC = None

_PR_SET_PDEATHSIG = 1


def _set_pdeathsig() -> None:
    """Ask the kernel to SIGKILL this child when its parent dies (Linux).

    Runs in the child between fork and exec. Guarantees the Stockfish process
    can never outlive the server, even on an unclean SIGKILL of the server. Only
    touches the already-resolved libc handle — no allocation, no dlopen.
    """
    if _LIBC is not None:
        _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


def _ensure_engine() -> chess.engine.SimpleEngine:
    """Return the running engine, starting it on first use. Call under _lock."""
    global _engine
    if _engine is None:
        path = _engine_path()
        if not os.path.exists(path):
            raise ToolError(
                f"Stockfish binary not found at '{path}'. Set STOCKFISH_PATH "
                "or place the binary at engine/stockfish."
            )
        _engine = chess.engine.SimpleEngine.popen_uci(path, preexec_fn=_set_pdeathsig)
        # Best-effort performance config; ignore options the build lacks.
        opts: dict[str, object] = {}
        threads = os.environ.get("STOCKFISH_THREADS")
        hash_mb = os.environ.get("STOCKFISH_HASH_MB")
        opts["Threads"] = int(threads) if threads else min(4, os.cpu_count() or 1)
        opts["Hash"] = int(hash_mb) if hash_mb else 256
        try:
            _engine.configure(opts)
        except Exception:
            pass
    return _engine


def _engine_pid() -> int | None:
    eng = _engine
    if eng is None:
        return None
    try:
        return eng.transport.get_pid()
    except Exception:
        return None


def _shutdown() -> None:
    """Stop the engine with a bounded, force-killing fallback (for atexit).

    A plain engine.quit() can block forever if it runs after python-chess's
    background event loop has already been torn down. So we grab the subprocess
    PID, attempt a graceful quit with a timeout, then SIGKILL the PID to
    guarantee the process never outlives us.
    """
    global _engine
    eng = _engine
    _engine = None
    if eng is None:
        return

    pid = None
    try:
        pid = eng.transport.get_pid()
    except Exception:
        pass

    quitter = threading.Thread(target=lambda: _safe_quit(eng), daemon=True)
    quitter.start()
    quitter.join(timeout=3.0)

    if pid is not None:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass  # already gone — graceful quit succeeded


def _safe_quit(eng: chess.engine.SimpleEngine) -> None:
    try:
        eng.quit()
    except Exception:
        pass


def _signal_handler(signum, frame):
    """Terminate fast and clean on SIGTERM/SIGINT (how MCP clients stop us).

    These bypass atexit, so we kill the engine child directly and exit. We skip
    the graceful UCI quit here because Stockfish is stateless — there's nothing
    to flush — and we want the host's shutdown to be snappy.
    """
    pid = _engine_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    os._exit(0)


def _install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported — atexit still covers us


atexit.register(_shutdown)


# --- Helpers -----------------------------------------------------------------
def _board(fen: str | None) -> chess.Board:
    """Build and validate a board from a FEN (default: starting position)."""
    if not fen or fen.strip().lower() in {"startpos", "start"}:
        return chess.Board()
    try:
        return chess.Board(fen.strip())
    except ValueError as exc:
        raise ToolError(f"Invalid FEN: {exc}")


def _format_score(score: chess.engine.PovScore) -> dict:
    """Normalize a PovScore into a White-POV dict that's easy to read."""
    white = score.white()
    if white.is_mate():
        mate_in = white.mate()
        eval_str = f"#{'+' if mate_in > 0 else '-'}{abs(mate_in)}"
        return {
            "type": "mate",
            "mate_in": mate_in,  # White's POV: + = White mates, - = Black mates
            "centipawns": None,
            "eval": eval_str,
            "leader": "white" if mate_in > 0 else "black",
        }
    cp = white.score(mate_score=MATE_SCORE)
    return {
        "type": "cp",
        "mate_in": None,
        "centipawns": cp,
        "eval": f"{cp / 100:+.2f}",  # pawns, White POV
        "leader": "white" if cp > 0 else "black" if cp < 0 else "equal",
    }


def _pv_to_san(board: chess.Board, pv: list[chess.Move]) -> list[str]:
    b = board.copy()
    out: list[str] = []
    for mv in pv:
        out.append(b.san(mv))
        b.push(mv)
    return out


def _status(board: chess.Board) -> dict:
    return {
        "turn": "white" if board.turn == chess.WHITE else "black",
        "fullmove_number": board.fullmove_number,
        "is_check": board.is_check(),
        "is_checkmate": board.is_checkmate(),
        "is_stalemate": board.is_stalemate(),
        "is_insufficient_material": board.is_insufficient_material(),
        "is_game_over": board.is_game_over(),
        "can_claim_draw": board.can_claim_draw(),
    }


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _build_limit(depth: int | None, movetime_ms: int | None) -> chess.engine.Limit:
    if movetime_ms is not None:
        return chess.engine.Limit(
            time=_clamp(movetime_ms, 1, MAX_MOVETIME_MS) / 1000.0
        )
    d = _clamp(depth if depth is not None else 18, 1, MAX_DEPTH)
    return chess.engine.Limit(depth=d)


# --- Tools -------------------------------------------------------------------
@mcp.tool
def analyze_position(
    fen: str | None = None,
    depth: int = 18,
    multipv: int = 1,
    movetime_ms: int | None = None,
) -> dict:
    """Analyze a chess position with Stockfish and return the evaluation and best lines.

    Args:
        fen: Position in FEN notation. Omit for the standard starting position.
        depth: Search depth (1-30). Ignored if movetime_ms is given.
        multipv: Number of top lines to return (1-10).
        movetime_ms: Search time budget in milliseconds (1-60000). Overrides depth.

    Returns the score (White's point of view), the best move, and the principal
    variation(s) in SAN, plus search metadata.
    """
    board = _board(fen)
    if board.is_game_over():
        return {
            "fen": board.fen(),
            "game_over": True,
            "result": board.result(),
            "status": _status(board),
            "lines": [],
        }

    multipv = _clamp(multipv, 1, MAX_MULTIPV)
    limit = _build_limit(depth, movetime_ms)

    with _lock:
        engine = _ensure_engine()
        info = engine.analyse(
            board, limit, multipv=multipv if multipv > 1 else None
        )

    infos = info if isinstance(info, list) else [info]
    lines = []
    for rank, entry in enumerate(infos, start=1):
        pv = entry.get("pv", [])
        lines.append(
            {
                "rank": rank,
                "score": _format_score(entry["score"]),
                "best_move_uci": pv[0].uci() if pv else None,
                "best_move_san": board.san(pv[0]) if pv else None,
                "pv_san": _pv_to_san(board, pv),
                "depth": entry.get("depth"),
                "nodes": entry.get("nodes"),
                "nps": entry.get("nps"),
            }
        )

    return {
        "fen": board.fen(),
        "game_over": False,
        "status": _status(board),
        "best_move_san": lines[0]["best_move_san"] if lines else None,
        "best_move_uci": lines[0]["best_move_uci"] if lines else None,
        "score": lines[0]["score"] if lines else None,
        "lines": lines,
    }


@mcp.tool
def get_best_move(
    fen: str | None = None,
    depth: int = 18,
    movetime_ms: int | None = None,
    skill_level: int | None = None,
    elo: int | None = None,
) -> dict:
    """Pick a move for the side to move, optionally at reduced strength.

    Args:
        fen: Position in FEN. Omit for the starting position.
        depth: Search depth (1-30). Ignored if movetime_ms is given.
        movetime_ms: Time budget in ms (1-60000). Overrides depth.
        skill_level: 0 (weakest) to 20 (full strength) for casual play.
        elo: Target playing strength in Elo (~1320-3190); enables UCI_LimitStrength.

    Returns the chosen move in UCI and SAN, plus the resulting FEN.
    """
    board = _board(fen)
    if board.is_game_over():
        raise ToolError(f"Game is already over ({board.result()}); no move to make.")

    limit = _build_limit(depth, movetime_ms)
    applied_opts: dict[str, object] = {}
    if skill_level is not None:
        applied_opts["Skill Level"] = _clamp(skill_level, 0, 20)
    if elo is not None:
        applied_opts["UCI_LimitStrength"] = True
        applied_opts["UCI_Elo"] = _clamp(elo, 1320, 3190)

    with _lock:
        engine = _ensure_engine()
        try:
            if applied_opts:
                engine.configure(applied_opts)
            result = engine.play(board, limit)
        finally:
            # Reset to full strength so weakening one call doesn't leak forward.
            if applied_opts:
                try:
                    engine.configure({"Skill Level": 20, "UCI_LimitStrength": False})
                except Exception:
                    pass

    move = result.move
    if move is None:
        raise ToolError("Engine returned no move.")
    san = board.san(move)
    board.push(move)
    return {
        "move_uci": move.uci(),
        "move_san": san,
        "resulting_fen": board.fen(),
        "status": _status(board),
    }


@mcp.tool
def evaluate_position(fen: str | None = None, depth: int = 12) -> dict:
    """Quickly evaluate a position and return the score and best move.

    A lighter, faster version of analyze_position (single line, shallower default
    depth). Score is from White's point of view.

    Args:
        fen: Position in FEN. Omit for the starting position.
        depth: Search depth (1-30), default 12.
    """
    board = _board(fen)
    if board.is_game_over():
        return {"fen": board.fen(), "game_over": True, "result": board.result()}

    limit = _build_limit(depth, None)
    with _lock:
        engine = _ensure_engine()
        info = engine.analyse(board, limit)
    pv = info.get("pv", [])
    return {
        "fen": board.fen(),
        "game_over": False,
        "score": _format_score(info["score"]),
        "best_move_san": board.san(pv[0]) if pv else None,
        "best_move_uci": pv[0].uci() if pv else None,
        "depth": info.get("depth"),
    }


@mcp.tool
def get_legal_moves(fen: str | None = None) -> dict:
    """List all legal moves in a position (SAN and UCI).

    Args:
        fen: Position in FEN. Omit for the starting position.
    """
    board = _board(fen)
    moves = [{"san": board.san(m), "uci": m.uci()} for m in board.legal_moves]
    return {
        "fen": board.fen(),
        "count": len(moves),
        "moves": moves,
        "status": _status(board),
    }


@mcp.tool
def apply_moves(moves: list[str], fen: str | None = None) -> dict:
    """Apply a sequence of moves to a position and return the resulting FEN.

    Useful for playing out a line without the server holding game state.

    Args:
        moves: Moves to play in order, each in SAN (e.g. "Nf3") or UCI (e.g. "g1f3").
        fen: Starting position in FEN. Omit for the standard starting position.

    Returns the resulting FEN, the moves played in SAN, an ASCII board, and status.
    Raises an error on the first illegal/unparseable move.
    """
    board = _board(fen)
    played: list[str] = []
    for raw in moves:
        token = raw.strip()
        move: chess.Move | None = None
        # Try UCI first, then SAN.
        try:
            cand = chess.Move.from_uci(token)
            if cand in board.legal_moves:
                move = cand
        except ValueError:
            move = None
        if move is None:
            try:
                move = board.parse_san(token)
            except ValueError:
                raise ToolError(
                    f"Illegal or unparseable move '{raw}' at position {board.fen()}"
                )
        played.append(board.san(move))
        board.push(move)

    return {
        "resulting_fen": board.fen(),
        "moves_played_san": played,
        "ascii": str(board),
        "status": _status(board),
        "result": board.result() if board.is_game_over() else None,
    }


@mcp.tool
def visualize_board(fen: str | None = None, unicode: bool = True) -> dict:
    """Render a position as a text board for human reading.

    Args:
        fen: Position in FEN. Omit for the starting position.
        unicode: Use Unicode chess piece glyphs (True) or ASCII letters (False).
    """
    board = _board(fen)
    diagram = board.unicode(borders=True) if unicode else str(board)
    return {
        "fen": board.fen(),
        "board": diagram,
        "turn": "white" if board.turn == chess.WHITE else "black",
        "status": _status(board),
    }


@mcp.tool
def engine_info() -> dict:
    """Return the Stockfish engine's name, author, and a few configurable options."""
    with _lock:
        engine = _ensure_engine()
        ident = dict(engine.id)
        keys = ("Threads", "Hash", "Skill Level", "UCI_Elo", "MultiPV")
        option_defaults = {
            k: engine.options[k].default for k in keys if k in engine.options
        }
    return {
        "id": ident,
        "option_defaults": option_defaults,  # engine defaults, not current values
        "binary": _engine_path(),
    }


def main() -> None:
    """Console-script entry point (``stockfish-mcp``). Runs the stdio server."""
    _install_signal_handlers()
    mcp.run()


if __name__ == "__main__":
    main()
