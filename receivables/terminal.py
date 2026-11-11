import argparse
import sys
from pathlib import Path
from .primitives import BookError, decode, canonical, error
from .service import Workbench
from .demonstration import run


def main(argv=None):
    parser = argparse.ArgumentParser(prog="receivables")
    parser.add_argument("--db", default="receivables.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo")
    execute = sub.add_parser("run")
    execute.add_argument("request")
    verify = sub.add_parser("verify")
    verify.add_argument("statement")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "demo":
            result = run()
        elif arguments.command == "verify":
            from .reporting.statements import Statements

            result = Statements.verify(arguments.statement)
        else:
            path = Path(arguments.request)
            if (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size > 2_000_000
            ):
                error("path", "request must be bounded local regular file")
            request = decode(path.read_text("utf-8"))
            with Workbench(arguments.db) as work:
                result = work.dispatch(request)
        print(canonical({"ok": True, "result": result}))
        return 0
    except BookError as exc:
        print(canonical({"ok": False, "error": exc.as_dict()}), file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            canonical({"ok": False, "error": {"code": "io", "message": str(exc)}}),
            file=sys.stderr,
        )
        return 3
