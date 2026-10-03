#!/usr/bin/env python3
"""Paper execution entry point at the upstream runner path.

The strategy runner shells out to ``<repo>/src/live/pm_live_trade_runner.py``
(the upstream author's private execution stack). Pointing ``--repo`` at this
checkout for paper trading lands here: a thin delegate to
``paper/pm_paper_trade_runner.py``, which simulates fills against the live
public CLOB book and REFUSES ``--execute`` (exit 2). Real execution still
requires the private stack.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from paper.pm_paper_trade_runner import main

if __name__ == "__main__":
    sys.exit(main())
