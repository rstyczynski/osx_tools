#!/bin/bash
# LaunchAgent and menu actions use the login environment, never interactive ~/.bashrc.
if [[ -r "$HOME/.bash_profile" ]]; then
  source "$HOME/.bash_profile"
fi
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || exit 1
exec /usr/bin/python3 "$DIR/tms-service.py" run "$@"
