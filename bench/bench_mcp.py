"""Measure what one MCP call costs an agent: latency and payload size.

Run it from the project you want to measure:

    python3 bench/bench_mcp.py [symbol]

The symbol defaults to one discovered from the graph, so the trace calls have something
real to walk.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
import mcp_server as m
import idxg

def pick_symbol():
    if len(sys.argv) > 1:
        return sys.argv[1]
    db = idxg.connect(None)
    row = db.execute("""SELECT name FROM symbols WHERE in_repo = 1 AND kind IN ('Class','Struct')
                        ORDER BY (in_deg + out_deg) DESC LIMIT 1""").fetchone()
    db.close()
    return row["name"] if row else "main"


SYM = pick_symbol()
print(f"project: {os.getcwd()}\nsymbol:  {SYM}\n")

CALLS = [
    ("index_status", {}),
    ("search_graph", {"name_pattern": f"^{SYM}$", "limit": 5}),
    ("search_graph", {"query": SYM, "limit": 10}),
    ("trace_path", {"symbol": SYM, "direction": "in", "depth": 2}),
    ("trace_path", {"symbol": SYM, "direction": "both", "depth": 2,
                    "edge_kinds": "CALLS,REFERENCES"}),
    ("find_references", {"symbol": SYM}),
    ("get_code_snippet", {"symbol": SYM}),
    ("get_architecture", {"limit": 5}),
    ("find_dead_code", {"limit": 20}),
    ("list_projects", {}),
    ("check_index_coverage", {"paths": ["."]}),
    ("query_graph", {"query": "SELECT kind, COUNT(*) n FROM symbols WHERE in_repo=1 "
                              "GROUP BY kind ORDER BY n DESC LIMIT 5"}),
    ("get_history", {"symbol": SYM}),
    ("get_history", {"limit": 30, "with_files": True}),
    ("get_history", {"limit": 10, "narrate": True}),
    ("get_digest", {}),
    ("get_churn", {}),
    ("get_timeline", {"periods": 3}),
    ("list_docs", {"kind": "readme"}),
    ("search_docs", {"query": "architecture"}),
]

print(f"{'tool':<22}{'ms':>7}{'bytes':>9}{'~tokens':>9}")
worst = []
for name, args in CALLS:
    t0 = time.time()
    try:
        out = m.call(name, args)
    except SystemExit as e:
        out = f"(SystemExit) {e}"
    ms = (time.time() - t0) * 1000
    approx = len(out) // 4
    label = name if len(name) < 22 else name[:21]
    print(f"{label:<22}{ms:>7.0f}{len(out):>9}{approx:>9}")
    worst.append((len(out), name, args))

worst.sort(reverse=True)
print(f"\nlargest payload: {worst[0][1]} {worst[0][2]}")
print("token figures are bytes/4, a rough stand-in for what the call costs an agent.")
