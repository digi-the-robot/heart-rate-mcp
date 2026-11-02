"""Run the bridge without installing it:  python run.py <command>

Same commands as the `hrbridge` CLI (run, mcp, status, stop, check, scan,
pulsoid-login, init). Handy for MCP client configs: point them at this file
with the argument `mcp`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hrbridge.cli import main  # noqa: E402

main()
