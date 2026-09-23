from pathlib import Path
import threading

from auction_probe import run_auction_probe

threading.Thread(
    target=run_auction_probe,
    daemon=True,
    name="auction-probe-v45",
).start()

_PARTS_DIR = Path(__file__).with_name("_v41_parts")
_SOURCE = "".join(
    (_PARTS_DIR / f"part{i:02d}.txt").read_text(encoding="utf-8")
    for i in range(1, 12)
)

exec(
    compile(_SOURCE, str(Path(__file__).with_name("bot_v41.py")), "exec"),
    globals(),
    globals(),
)
