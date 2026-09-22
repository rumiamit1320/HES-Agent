"""Windows/PyInstaller entry point for HES Power Outage Agent.

This imports the real package entry point so package-relative imports in
hes_agent.agent remain valid when the application is frozen.
"""

import sys

from hes_agent.agent import main


if __name__ == "__main__":
    sys.exit(main())
