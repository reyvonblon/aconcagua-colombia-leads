#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/Users/raymondvonblon/.gemini/antigravity/scratch/aconcagua-leads"
PLIST_PATH="$HOME/Library/LaunchAgents/com.aconcagua.colombia.leads.plist"
LOG_DIR="$PROJECT_DIR/data"
mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.aconcagua.colombia.leads</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$PROJECT_DIR/webapp/server.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$PROJECT_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ACONCAGUA_HOST</key>
    <string>127.0.0.1</string>
    <key>ACONCAGUA_PORT</key>
    <string>8796</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/server.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/server.err.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"
echo "Servicio com.aconcagua.colombia.leads activo en http://127.0.0.1:8796/admin"
