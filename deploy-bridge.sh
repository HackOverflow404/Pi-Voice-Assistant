#!/usr/bin/env bash
# Build the WhatsApp bridge on this machine and install it on the Pi as the wa-bridge
# user service. Pairing is separate; see README.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
pi_host=${PI_HOST:-mainframe}
case ${1:-} in
  '') ./build-bridge.sh ;;
  --no-build) [[ -f dist/wa-bridge ]] || { echo 'No dist/wa-bridge; run without --no-build.' >&2; exit 1; } ;;
  *) echo 'Usage: ./deploy-bridge.sh [--no-build]   (env: PI_HOST)' >&2; exit 2 ;;
esac
ssh "$pi_host" 'mkdir -p ~/.local/share/pi-voice-assistant/bin ~/.config/systemd/user'
scp dist/wa-bridge "$pi_host:.local/share/pi-voice-assistant/bin/wa-bridge.new"
scp systemd/wa-bridge.service "$pi_host:.config/systemd/user/wa-bridge.service"
ssh "$pi_host" 'set -e
  cd ~/.local/share/pi-voice-assistant
  chmod 755 bin/wa-bridge.new && mv bin/wa-bridge.new bin/wa-bridge
  systemctl --user daemon-reload
  systemctl --user enable wa-bridge.service
  systemctl --user restart wa-bridge.service
  sleep 2
  curl -fsS http://127.0.0.1:8766/status; echo'
