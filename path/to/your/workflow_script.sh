# Check and source tg_helpers.sh
if [ -f "scripts/tg_helpers.sh" ]; then
    source scripts/tg_helpers.sh
else
    echo "Warning: tg_helpers.sh not found, fallback functions will be used."
fi

# No-op fallback function for tg_send_tracked
function tg_send_tracked() {
    echo "No-op: tg_send_tracked called but helpers unavailable."
}

# Check and source gh_helpers.sh
if [ -f "scripts/gh_helpers.sh" ]; then
    source scripts/gh_helpers.sh
else
    echo "Warning: gh_helpers.sh not found, fallback functions will be used."
fi

# Guard for helper function calls or redefine
function some_notification_function() {
    if type tg_send_tracked &>/dev/null; then
        tg_send_tracked "message"
    else
        echo "Warning: tg_send_tracked is not available."
    fi
}
