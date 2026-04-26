"""
Strategy Promotion / Demotion Tool
====================================
Usage:
  python promote.py lab→live my_strategy.py      Promote from lab to live
  python promote.py live→archive my_strategy.py  Archive a live strategy
  python promote.py lab→archive my_strategy.py   Archive a failed experiment
  python promote.py list                          Show all strategies by status
"""
import sys
import os
import shutil
from datetime import datetime


PATHS = {
    "live": "strategies/live",
    "lab": "strategies/lab",
    "archive": "strategies/archive",
}

CRITERIA = """
Promotion criteria (lab → live):
  ✓ Backtest profit factor > 1.3
  ✓ Win rate > 45%
  ✓ Max drawdown < 15%
  ✓ At least 30 trades in backtest
  ✓ Tested on 6+ months of data
"""


def list_strategies():
    print("\n" + "="*50)
    for status, path in PATHS.items():
        files = [f for f in os.listdir(path) if f.endswith(".py") and f != "__init__.py" and f != "TEMPLATE.py"]
        label = {"live": "LIVE (deployed)", "lab": "LAB (testing)", "archive": "ARCHIVE (failed)"}[status]
        print(f"\n  [{label}]")
        if files:
            for f in files:
                print(f"    • {f}")
        else:
            print("    (empty)")
    print()


def promote(from_status: str, to_status: str, filename: str):
    src = os.path.join(PATHS[from_status], filename)
    dst_dir = PATHS[to_status]
    dst = os.path.join(dst_dir, filename)

    if not os.path.exists(src):
        print(f"Error: {src} not found")
        return

    # If archiving, add date prefix
    if to_status == "archive":
        date_prefix = datetime.now().strftime("%Y-%m-%d_")
        dst = os.path.join(dst_dir, date_prefix + filename)

    shutil.copy2(src, dst)
    os.remove(src)

    print(f"\n  Moved: {src}")
    print(f"  To:    {dst}")

    if to_status == "live":
        print(f"\n{CRITERIA}")
        print("  Remember to add to STRATEGY_MAP in bot/trader.py")
    elif to_status == "archive":
        print("\n  Add a note to strategies/archive/README.md explaining why it failed.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit()

    if sys.argv[1] == "list":
        list_strategies()
    elif len(sys.argv) == 3:
        move = sys.argv[1]
        filename = sys.argv[2]
        parts = move.split("→")
        if len(parts) == 2 and parts[0] in PATHS and parts[1] in PATHS:
            promote(parts[0], parts[1], filename)
        else:
            print(f"Invalid move: {move}")
            print(__doc__)
    else:
        print(__doc__)
