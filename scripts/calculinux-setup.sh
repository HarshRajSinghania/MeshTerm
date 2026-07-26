#!/bin/sh
# calculinux-setup.sh -- one-time bring-up of MeshTerm on a Luckfox Lyra / PicoCalc
# running Calculinux (Yocto, systemd, read-only root with /usr + /etc + /home overlays
# backed by /data). Run as root on a freshly-flashed SD, over the serial console is fine:
#
#     sh calculinux-setup.sh
#
# It is idempotent -- safe to re-run; each phase checks before it acts. It sequences the
# whole deploy this repo's Lyra target needed, in the order the discoveries fell out:
#
#   1. opkg packages   Calculinux ships a STRIPPED python3.13 (no pip/venv/ensurepip and
#                      missing stdlib: sqlite3, ctypes, curses, shlex, xml). /usr is a
#                      writable overlay, so `opkg install python3-modules python3-pip`
#                      restores the full interpreter and persists on /data.
#   2. wi-fi kick      The USB RTL8188EU (rtl8xxxu) firmware finishes loading AFTER iwd's
#                      first scans, so on cold boot wlan0 stays UP/NO-CARRIER and the known
#                      network is never joined. A oneshot service drives RAW `iw` scans
#                      until a DHCP lease -- a completed raw scan is what lets iwd associate.
#   3. deploy user     The `meshterm` login MeshTerm runs under (created if absent; no password is
#                      set here -- run `passwd meshterm` yourself).
#   4. clone           Pull MeshTerm with the READ-ONLY GitHub deploy key over SSH.
#   5. venv + install  A venv + `pip install -e .`. pip's C builds hit ENOSPC because /tmp
#                      is a tiny RAM tmpfs, so TMPDIR is redirected to $HOME/tmp on /data.
#   6. PATH            Put the venv's `meshterm` on meshterm's login PATH via ~/.profile.
#   7. console font    Hand off to calculinux-console-font.sh (braille + node glyphs +
#                      rounded frame corners + the list cursor the bare console can't draw).
#
# Two prerequisites this script cannot safely embed and will check for / guide you through:
#   * the read-only deploy key at $HOME/.ssh/id_ed25519 (never commit a private key);
#   * Wi-Fi credentials known to iwd. The kick only nudges an ALREADY-known network. Either
#     provision it once by hand (`iwctl station wlan0 connect <SSID>`), or export
#     WIFI_SSID and WIFI_PSK before running and this script writes the iwd config for you.
set -eu

# --- knobs (override via the environment) ----------------------------------------------
DEPLOY_USER="${DEPLOY_USER:-meshterm}"
REPO_SSH="${REPO_SSH:-git@github.com:jpmartineau/MeshTerm.git}"
KEY_PATH="${KEY_PATH:-/home/$DEPLOY_USER/.ssh/id_ed25519}"
CHECKOUT="/home/$DEPLOY_USER/MeshTerm"
WIFI_SSID="${WIFI_SSID:-}"     # optional: set both to have iwd credentials written
WIFI_PSK="${WIFI_PSK:-}"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

log()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
runas() { su - "$DEPLOY_USER" -c "$1"; }

[ "$(id -u)" = 0 ] || die "run as root (installs packages, writes /etc, creates the user)"

# --- 1. opkg packages ------------------------------------------------------------------
log "1/7  system packages (opkg)"
if have opkg; then
    opkg update >/dev/null 2>&1 || info "opkg update failed (no network yet?) -- continuing"
    # git and kbd ship in the base image; the python pieces are what the stripped build drops.
    for pkg in python3-modules python3-pip git kbd; do
        if opkg status "$pkg" 2>/dev/null | grep -q '^Status:.*installed'; then
            info "$pkg already installed"
        else
            info "installing $pkg"
            opkg install "$pkg" || die "opkg install $pkg failed (need network for the feed)"
        fi
    done
else
    info "opkg not found -- assuming packages are already present"
fi
have python3 || die "python3 still missing after opkg (check the opkg feed / network)"

# --- 2. wi-fi boot-scan kick (rtl8xxxu race workaround) --------------------------------
log "2/7  wi-fi boot-scan kick"

# Optionally provision the iwd network so the kick has something known to join. Secrets
# come from the environment only -- nothing is written to disk from this repo.
if [ -n "$WIFI_SSID" ] && [ -n "$WIFI_PSK" ]; then
    info "writing iwd credentials for '$WIFI_SSID'"
    mkdir -p /var/lib/iwd
    printf '[Security]\nPassphrase=%s\n' "$WIFI_PSK" > "/var/lib/iwd/$WIFI_SSID.psk"
    chmod 600 "/var/lib/iwd/$WIFI_SSID.psk"
elif [ -n "$WIFI_SSID" ] || [ -n "$WIFI_PSK" ]; then
    info "WIFI_SSID and WIFI_PSK must BOTH be set to write credentials -- skipping"
else
    info "no WIFI_SSID/WIFI_PSK given -- the kick will nudge whatever iwd already knows"
fi

cat > /etc/wifi-kick.sh <<'KICKEOF'
#!/bin/sh
# rtl8xxxu boot workaround.
#
# On cold boot iwd comes up before the RTL8188EU firmware finishes loading and
# its own scans then return nothing, so wlan0 stays UP/NO-CARRIER and the known
# network (stored PSK) is never joined. A *raw* "iw dev wlan0 scan" does work,
# and a completed raw scan populates the mac80211 results, after which iwd
# immediately authenticates/associates to the known network on its own.
#
# So we just drive raw scans until we get a DHCP lease. Do NOT use
# "iwctl scan" here: it makes iwd grab the device and the raw scan then fails
# busy (that is why a scan-only nudge did not work).

has_ip() {
    ip -4 addr show wlan0 2>/dev/null | grep -q "inet "
}

# Wait for the dongle to enumerate.
n=0
while [ $n -lt 60 ]; do
    [ -d /sys/class/net/wlan0 ] && break
    sleep 1
    n=$((n + 1))
done

# Drive raw scans until iwd associates and DHCP lands (~5 min ceiling).
i=0
while [ $i -lt 30 ]; do
    has_ip && exit 0
    /usr/sbin/ip link set wlan0 up 2>/dev/null
    /usr/sbin/iw dev wlan0 scan >/dev/null 2>&1
    # Association + DHCP follow a completed scan; poll before scanning again.
    j=0
    while [ $j -lt 5 ]; do
        sleep 2
        has_ip && exit 0
        j=$((j + 1))
    done
    i=$((i + 1))
done
exit 0
KICKEOF
chmod +x /etc/wifi-kick.sh

cat > /etc/systemd/system/wifi-kick.service <<'UNITEOF'
[Unit]
Description=Nudge iwd to reconnect wlan0 at boot (rtl8xxxu workaround)
After=iwd.service
Wants=iwd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/etc/wifi-kick.sh

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable wifi-kick.service >/dev/null 2>&1 || info "could not enable wifi-kick.service"
info "installed /etc/wifi-kick.sh + wifi-kick.service (enabled)"

# --- 3. deploy user --------------------------------------------------------------------
log "3/7  deploy user '$DEPLOY_USER'"
if id "$DEPLOY_USER" >/dev/null 2>&1; then
    info "user exists"
else
    info "creating user"
    if have useradd; then
        useradd -m -s /bin/bash "$DEPLOY_USER" || die "useradd failed"
    else
        adduser -D -s /bin/bash "$DEPLOY_USER" || die "adduser failed"
    fi
    # wheel -> sudo, matching the working device; harmless if the group is absent.
    (usermod -aG wheel "$DEPLOY_USER" 2>/dev/null || adduser "$DEPLOY_USER" wheel 2>/dev/null) || true
    info "no password set -- run:  passwd $DEPLOY_USER"
fi

# --- 4. clone MeshTerm (read-only deploy key over SSH) ---------------------------------
log "4/7  clone MeshTerm"
[ -f "$KEY_PATH" ] || die "deploy key not found at $KEY_PATH
   place the READ-ONLY GitHub deploy key there first, e.g.:
     install -d -m700 -o $DEPLOY_USER -g $DEPLOY_USER /home/$DEPLOY_USER/.ssh
     cp id_ed25519 $KEY_PATH && chmod 600 $KEY_PATH
     chown $DEPLOY_USER:$DEPLOY_USER $KEY_PATH"

KNOWN_HOSTS="/home/$DEPLOY_USER/.ssh/known_hosts"
SSH_CMD="ssh -i $KEY_PATH -o IdentitiesOnly=yes -o UserKnownHostsFile=$KNOWN_HOSTS"
# Pin github.com's host key up front so the clone never blocks on an interactive prompt.
if ! runas "test -f $KNOWN_HOSTS && grep -q github.com $KNOWN_HOSTS"; then
    info "recording github.com host key"
    runas "ssh-keyscan -t ed25519 github.com >> $KNOWN_HOSTS 2>/dev/null" || info "ssh-keyscan failed (offline?)"
fi

if [ -d "$CHECKOUT/.git" ]; then
    info "already cloned -- pulling"
    runas "git -C ~/MeshTerm pull --ff-only" || info "pull failed (offline?) -- continuing"
else
    info "cloning $REPO_SSH"
    runas "GIT_SSH_COMMAND='$SSH_CMD' git clone $REPO_SSH ~/MeshTerm" \
        || die "clone failed (key not authorized, or offline)"
    # Bake the deploy key into the checkout so later pulls just work.
    runas "git -C ~/MeshTerm config core.sshCommand '$SSH_CMD'"
fi

# --- 5. venv + editable install (TMPDIR off the RAM tmpfs) -----------------------------
log "5/7  python venv + install"
if runas "test -x ~/MeshTerm/.venv/bin/python"; then
    info "venv exists"
else
    info "creating venv"
    runas "python3 -m venv ~/MeshTerm/.venv" || die "venv creation failed"
fi
info "pip install -e . (TMPDIR on /data to dodge the /tmp ENOSPC)"
runas "mkdir -p ~/tmp && cd ~/MeshTerm && TMPDIR=\$HOME/tmp .venv/bin/pip install -e ." \
    || die "pip install failed"

# --- 6. PATH (login shells) ------------------------------------------------------------
log "6/7  login PATH"
PROFILE="/home/$DEPLOY_USER/.profile"
if [ -f "$PROFILE" ] && grep -q 'MeshTerm/.venv/bin' "$PROFILE"; then
    info "already on PATH"
else
    info "adding venv to PATH in ~/.profile"
    printf '\n# MeshTerm venv on PATH\nexport PATH="$HOME/MeshTerm/.venv/bin:$PATH"\n' >> "$PROFILE"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$PROFILE"
fi

# --- 7. console font -------------------------------------------------------------------
log "7/7  console font"
if [ -f "$SCRIPT_DIR/calculinux-console-font.sh" ]; then
    sh "$SCRIPT_DIR/calculinux-console-font.sh"
else
    info "calculinux-console-font.sh not alongside this script -- skipping (run it separately)"
fi

# --- done ------------------------------------------------------------------------------
log "done"
info "log in as $DEPLOY_USER and run 'meshterm' (live) or 'meshterm --mock' (simulator)."
[ -n "$WIFI_SSID" ] || info "if wi-fi never comes up on boot, provision it once: iwctl station wlan0 connect <SSID>"
