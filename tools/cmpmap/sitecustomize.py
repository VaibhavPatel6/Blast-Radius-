"""Turns on the comparison-type recorder (cmprec.py) in any Python process started with
CMPMAP_DIR set."""
import os

if os.environ.get("CMPMAP_DIR"):
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cmprec

    cmprec.install()
