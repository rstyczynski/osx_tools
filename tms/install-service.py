#!/usr/bin/python3
"""Compatibility entry point. All service changes require an explicit feature/action."""
import sys
from tms_manage import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("TMS setup error: " + str(error), file=sys.stderr)
        sys.exit(1)
