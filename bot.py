from pathlib import Path

import v44_cookie_patch  # patches avito_provider before bot imports it

_PARTS_DIR = Path(__file__).with_name("_v41_parts")
_SOURCE = "".join(
    (_PARTS_DIR / f"part{i:02d}.txt").read_text(encoding="utf-8")
    for i in range(1, 12)
)
_SOURCE = _SOURCE.replace('APP_VERSION = "v43"', 'APP_VERSION = "v44"', 1)

exec(
    compile(_SOURCE, str(Path(__file__).with_name("bot_v44.py")), "exec"),
    globals(),
    globals(),
)
