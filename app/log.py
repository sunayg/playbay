import sys
import traceback
from collections.abc import Callable
from typing import Any

import settings


def debug(msg: str) -> None:
    with open(settings.LOGFILE, "a+") as f:
        f.write(msg + "\n")


def event(msg: str) -> None:
    """A line for the log file and for the container's own log (`docker logs`): things an operator should see."""
    debug(msg)
    print(msg, file=sys.stderr, flush=True)


def write_log() -> None:
    with open(settings.LOGFILE, "a+") as f:
        f.write(traceback.format_exc() + "\n")


def catch_and_log_exceptions[F: Callable[..., Any]](fn: F) -> F:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            write_log()
            return None

    return wrapper  # type: ignore
