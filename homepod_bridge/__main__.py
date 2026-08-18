import sys


def _record_crash() -> None:
    """Last-resort breadcrumb: under pythonw there is no console, and an
    ImportError here happens before logging is configured - without this a
    broken install dies at login with zero diagnostics anywhere."""
    import datetime
    import traceback

    try:
        from .logging_setup import log_directory  # stdlib-only import chain

        directory = log_directory()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(directory / "crash.log", "a", encoding="utf-8") as fh:
            fh.write(f"\n--- {stamp} ---\n{traceback.format_exc()}")
    except Exception:  # noqa: BLE001 - never mask the original failure
        pass


try:
    from .cli import main

    code = main()
except SystemExit:
    raise
except Exception:
    _record_crash()
    raise
sys.exit(code)
