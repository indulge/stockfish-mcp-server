"""End-to-end smoke test: drive the MCP server through an in-memory client."""

import asyncio
import json

from fastmcp import Client

from server import mcp


async def main() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        print("TOOLS:", sorted(t.name for t in tools))

        async def call(name, **kwargs):
            res = await client.call_tool(name, kwargs)
            return res.data

        print("\n[engine_info]")
        print(json.dumps(await call("engine_info"), indent=2))

        print("\n[analyze_position startpos depth 14, multipv 2]")
        out = await call("analyze_position", depth=14, multipv=2)
        print("best:", out["best_move_san"], out["score"]["eval"])
        for ln in out["lines"]:
            print(f"  #{ln['rank']}", ln["best_move_san"], ln["score"]["eval"],
                  "pv:", " ".join(ln["pv_san"][:5]))

        # Mate-in-1: White to move, Qxf7#
        mate_fen = "rnbqkbnr/pppp1ppp/8/4p3/2B1P3/8/PPPP1PPP/RNBQK1NR w KQkq - 0 1"
        # Scholar's mate setup -> use a known M1: Fool's-mate-ish. Use a clean M1.
        m1 = "6k1/5ppp/8/8/8/8/5PPP/4R1K1 w - - 0 1"  # Re8#
        print("\n[analyze_position mate-in-1]", m1)
        out = await call("analyze_position", fen=m1, depth=10)
        print("  best:", out["best_move_san"], "score:", out["score"])

        print("\n[get_best_move at skill_level 3]")
        out = await call("get_best_move", depth=10, skill_level=3)
        print("  ", out["move_san"], out["move_uci"])

        print("\n[get_best_move at elo 1500]")
        out = await call("get_best_move", depth=12, elo=1500)
        print("  ", out["move_san"], out["move_uci"])

        print("\n[apply_moves 1.e4 e5 2.Nf3]")
        out = await call("apply_moves", moves=["e4", "e5", "Nf3"])
        print("  fen:", out["resulting_fen"])
        print("  san:", out["moves_played_san"])

        print("\n[get_legal_moves startpos]")
        out = await call("get_legal_moves")
        print("  count:", out["count"])

        print("\n[apply_moves illegal -> should error]")
        try:
            await call("apply_moves", moves=["e4", "e4"])
            print("  ERROR: did not raise")
        except Exception as e:
            print("  raised as expected:", str(e)[:80])

        print("\n[analyze bad FEN -> should error]")
        try:
            await call("analyze_position", fen="not-a-fen")
            print("  ERROR: did not raise")
        except Exception as e:
            print("  raised as expected:", str(e)[:80])

        print("\n[visualize_board startpos]")
        out = await call("visualize_board", unicode=False)
        print(out["board"])

        print("\nALL CHECKS DONE")


if __name__ == "__main__":
    asyncio.run(main())
    # The shared in-process engine keeps python-chess's background event-loop
    # thread alive, which would block a normal interpreter exit. The real server
    # never exits this way (it stops via SIGTERM/SIGINT/SIGKILL), so just force
    # a clean exit here now that all checks have passed.
    import os

    os._exit(0)
