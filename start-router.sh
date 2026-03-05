#!/bin/bash
# Start the Rob Router daemon in a tmux session called 'rob-router'.
# Usage: ./start-router.sh

set -e

SESSION="rob-router"
ROUTER_DIR="/opt/rob-router"

# Kill existing session if running
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Stopping existing $SESSION session..."
    tmux kill-session -t "$SESSION"
    sleep 1
fi

# Create state dirs
mkdir -p "$ROUTER_DIR/state/descs"
mkdir -p "$ROUTER_DIR/sessions"

# Start in tmux
echo "Starting Rob Router in tmux session '$SESSION'..."
tmux new-session -d -s "$SESSION" -x 220 -y 50 \
    "cd $ROUTER_DIR && uv run python3 main.py 2>&1 | tee -a state/router.log"

echo "Started. Monitor with: tmux attach -t $SESSION"
echo "Logs at: $ROUTER_DIR/state/router.log"
