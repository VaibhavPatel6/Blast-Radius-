"""Turns on recording in any Python process started with COVMAP_DIR set (pool workers,
`python -c` subprocesses), and writes its record there at exit."""
import os

if os.environ.get("COVMAP_DIR"):
    import atexit
    import json
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import recorder

    _rec = recorder.Recorder("/src/")
    _rec.start()

    def _write():
        _rec.stop()
        path = os.path.join(os.environ["COVMAP_DIR"], f"child-{os.getpid()}.json")
        with open(path + ".tmp", "w") as fh:
            json.dump(_rec.result(), fh)
        os.rename(path + ".tmp", path)

    atexit.register(_write)
