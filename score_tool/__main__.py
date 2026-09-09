import datetime
import traceback
from pathlib import Path


def main() -> None:
    try:
        from score_tool.app import main as _main

        _main()
    except Exception:
        # pythonw 无控制台，启动崩溃时把异常写入日志文件
        log = Path(__file__).resolve().parent.parent / "gui_error.log"
        try:
            with log.open("a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.datetime.now()} ===\n")
                f.write(traceback.format_exc())
        except OSError:
            pass
        raise


main()
