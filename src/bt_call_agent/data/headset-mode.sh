#!/usr/bin/env bash
# Switch this Linux machine's Bluetooth into "headset" mode (HFP Hands-Free +
# A2DP sink), so a phone treats it like a Bluetooth headset — and back.
#
#   headset-mode.sh enable | disable | status
#
# What `enable` changes:
#   1. ~/.config/wireplumber/.../60-bt-call-agent-hfp.conf   bluez5.roles = [ hfp_hf a2dp_sink ]
#   2. /etc/bluetooth/main.conf                                Class = 0x240404 (Audio/Video, Wearable Headset)
#                                                              Name  = <hostname> (if unset)
#   3. bluetooth.service drop-in                               bluetoothd -P hostname
#      (the hostname plugin otherwise overrides the class with "Computer")
# While enabled, this machine can't send its own audio to Bluetooth headphones.
set -euo pipefail

WP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d"
WP_CONF="$WP_DIR/60-bt-call-agent-hfp.conf"
WP_HEADLESS="$WP_DIR/60-bt-call-agent-headless.conf"
DROPIN_DIR=/etc/systemd/system/bluetooth.service.d
DROPIN="$DROPIN_DIR/60-bt-call-agent.conf"
MAIN=/etc/bluetooth/main.conf
BACKUP=/etc/bluetooth/main.conf.bt-call-agent.bak
CLASS=0x240404

# Does this user lack a logind session on a seat (no local desktop)? WirePlumber
# runs its BlueZ monitor only for an active seat, so headless boxes need seat
# monitoring off. Override with BT_CALL_AGENT_HEADLESS=1 / 0.
is_headless() {
  case "${BT_CALL_AGENT_HEADLESS:-}" in 1) return 0 ;; 0) return 1 ;; esac
  local s
  for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk -v u="$USER" '$3==u {print $1}'); do
    [ -n "$(loginctl show-session "$s" -p Seat --value 2>/dev/null)" ] && return 1
  done
  return 0
}

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }

# Effective bluetoothd command line, e.g. "/usr/lib/bluetooth/bluetoothd -P foo"
bluetoothd_argv() {
  systemctl show -p ExecStart --value bluetooth.service \
    | sed -n 's/.*argv\[\]=\([^;]*\);.*/\1/p' | head -1 | sed 's/[[:space:]]*$//'
}

set_main_kv() {  # key value  — set in [General], replacing a commented default if present
  local key=$1 val=$2
  if sudo grep -qE "^[[:space:]]*#?[[:space:]]*${key}[[:space:]]*=" "$MAIN"; then
    sudo sed -i -E "0,/^[[:space:]]*#?[[:space:]]*${key}[[:space:]]*=.*/s//${key} = ${val}/" "$MAIN"
  elif sudo grep -q '^\[General\]' "$MAIN"; then
    sudo sed -i "/^\[General\]/a ${key} = ${val}" "$MAIN"
  else
    printf '[General]\n%s = %s\n' "$key" "$val" | sudo tee -a "$MAIN" >/dev/null
  fi
}

restart_stack() {
  say "restarting bluetooth and wireplumber"
  sudo systemctl daemon-reload
  sudo systemctl restart bluetooth
  systemctl --user restart wireplumber
  sleep 2
}

enable() {
  command -v bluetoothctl >/dev/null || { warn "bluez is not installed"; exit 1; }
  command -v wpctl >/dev/null || { warn "pipewire/wireplumber is not installed"; exit 1; }
  have_spa_bluez || { warn "PipeWire's Bluetooth plugin is missing (Debian/Ubuntu: libspa-0.2-bluetooth)"; exit 1; }

  for f in "$WP_DIR"/*.conf; do
    [ -e "$f" ] && [ "$f" != "$WP_CONF" ] && grep -q 'bluez5.roles' "$f" \
      && warn "$f also sets bluez5.roles; the later file (by name) wins"
  done
  say "WirePlumber: Hands-Free + A2DP sink roles -> $WP_CONF"
  mkdir -p "$WP_DIR"
  cat > "$WP_CONF" <<'EOF'
## bt-call-agent: act as a Bluetooth headset for a phone (HFP Hands-Free + A2DP sink).
## While this file exists, this machine can't play its own audio to BT headphones.
monitor.bluez.properties = {
  bluez5.roles = [ hfp_hf a2dp_sink ]
  bluez5.hfphsp-backend = "native"
}
EOF

  if is_headless; then
    say "no local seat for $USER (headless): disabling WirePlumber's BlueZ seat monitoring -> $WP_HEADLESS"
    cat > "$WP_HEADLESS" <<'EOF'
## bt-call-agent: headless machine. WirePlumber runs its BlueZ monitor only for an
## active logind seat; without one, no Bluetooth audio profiles get registered.
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF
  else
    rm -f "$WP_HEADLESS"
  fi

  say "BlueZ: device class $CLASS (headset) in $MAIN"
  sudo test -f "$MAIN" || printf '[General]\n' | sudo tee "$MAIN" >/dev/null
  sudo test -f "$BACKUP" || sudo cp -a "$MAIN" "$BACKUP"
  set_main_kv Class "$CLASS"
  sudo grep -qE '^[[:space:]]*Name[[:space:]]*=' "$MAIN" || set_main_kv Name "$(hostname)"

  local argv; argv=$(bluetoothd_argv)
  if [ -z "$argv" ]; then
    warn "couldn't read bluetoothd's command line; add '-P hostname' to it yourself"
  elif [[ "$argv" == *hostname* ]]; then
    say "bluetoothd already runs without the hostname plugin ($argv)"
  elif [[ "$argv" =~ (^|[[:space:]])(-P|--noplugin) ]]; then
    warn "bluetoothd already has -P/--noplugin ($argv); add 'hostname' to that list yourself"
  else
    say "bluetoothd: disable hostname plugin -> $DROPIN"
    sudo mkdir -p "$DROPIN_DIR"
    printf '# bt-call-agent: keep the headset device class (hostname plugin would force "Computer")\n[Service]\nExecStart=\nExecStart=%s -P hostname\n' \
      "$argv" | sudo tee "$DROPIN" >/dev/null
  fi

  restart_stack
  status
  echo
  say "done. Next: bt-call-agent pair"
}

disable() {
  say "removing headset mode"
  rm -f "$WP_CONF" "$WP_HEADLESS"
  sudo rm -f "$DROPIN"
  if sudo test -f "$BACKUP"; then
    sudo mv "$BACKUP" "$MAIN"
  else
    warn "no backup of $MAIN found; leaving it as is"
  fi
  restart_stack
  status
}

have_spa_bluez() {
  compgen -G "/usr/lib*/spa-0.2/bluez5/libspa-bluez5.so" >/dev/null \
    || compgen -G "/usr/lib/*/spa-0.2/bluez5/libspa-bluez5.so" >/dev/null
}

status() {
  if ! command -v bluetoothctl >/dev/null; then
    warn "BlueZ is not installed (no bluetoothctl). Run: bt-call-agent doctor"
    return 1
  fi
  local show; show=$(bluetoothctl show 2>/dev/null || true)
  echo "WirePlumber roles file : $([ -f "$WP_CONF" ] && echo present || echo absent)"
  echo "headless seat override : $([ -f "$WP_HEADLESS" ] && echo present || echo absent)"
  echo "bluetoothd drop-in     : $([ -f "$DROPIN" ] && echo present || echo absent)"
  echo "bluetoothd command     : $(bluetoothd_argv)"
  echo "main.conf Class/Name   : $(grep -E '^[[:space:]]*(Class|Name)[[:space:]]*=' "$MAIN" 2>/dev/null | tr '\n' ' ' || true)"
  echo "adapter powered        : $(grep -oP 'Powered: \K\w+' <<<"$show" || echo '?')"
  echo "adapter class          : $(grep -oP 'Class: \K0x\w+' <<<"$show" || echo '?')"
  if grep -q 0000111e <<<"$show"; then
    echo "role                   : Hands-Free (headset) ✓"
  elif grep -q 0000111f <<<"$show"; then
    echo "role                   : Audio Gateway (normal PC)"
  else
    echo "role                   : none — no HFP profile registered (adapter off, or WirePlumber's BlueZ monitor isn't running; headless? re-run setup enable)"
  fi
}

case "${1:-}" in
  enable) enable ;;
  disable) disable ;;
  status) status ;;
  *) echo "usage: $0 enable|disable|status" >&2; exit 2 ;;
esac
