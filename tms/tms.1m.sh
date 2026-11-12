#!/bin/bash
# <xbar.title>TimeMachine Simplified</xbar.title>
# <xbar.version>1.5.1</xbar.version>
# <xbar.desc>Backup status, reports, and backup-now actions.</xbar.desc>
SCRIPT=$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$0") || exit 1
exec /usr/bin/python3 "$(dirname "$SCRIPT")/tms-service.py" menu
