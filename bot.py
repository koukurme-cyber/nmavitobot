from pathlib import Path

_PARTS_DIR = Path(__file__).with_name("_v36_parts")
_SOURCE = "".join(
    (_PARTS_DIR / f"part{i:02d}.txt").read_text(encoding="utf-8")
    for i in range(1, 9)
)

exec(
    compile(_SOURCE, str(Path(__file__).with_name("bot_v37.py")), "exec"),
    globals(),
    globals(),
)
