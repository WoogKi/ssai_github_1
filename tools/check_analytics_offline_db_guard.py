"""Prove an offline Analytics gate aborts before an ODBC connection opens."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
from unittest.mock import patch

import pyodbc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import check_analytics_regression as gate


def main() -> int:
    output = StringIO()
    with patch.object(sys, "argv", ["check_analytics_regression.py"]), \
         patch.object(gate, "run_basic_checks", side_effect=lambda: pyodbc.connect("offline-fixture")), \
         redirect_stdout(output):
        result = gate.main()
    assert result == 1
    assert "DB connection attempted by offline Analytics gate" in output.getvalue()
    print("PASS Analytics offline entry aborts before ODBC connect; no DB called")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
