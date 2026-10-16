#!/bin/bash
# Run simulator commands on the runner host in a detached tmux session, with a log (M4-1).
# Run it as ec2-user after setup.sh:
#
#     bash in-tmux.sh <session> <simulator arguments...> [-- <simulator arguments...>]...
#
#     bash in-tmux.sh batch run-batch --queue simulator/queues/m4-1-validation.json
#
# Each `--` starts another command; they run one after another, and a failed
# one stops the rest. The session keeps going when your Session Manager session
# drops. Watch it with `tmux attach -t <session>` (detach: Ctrl-b d) or
# `tail -f ~/logs/<session>-*.log`.
set -euo pipefail

SESSION="${1:?usage: in-tmux.sh <session> <simulator arguments...> [-- <more arguments...>]}"
shift
LOG="$HOME/logs/$SESSION-$(date -u +%Y%m%dT%H%M%SZ).log"
mkdir -p "$HOME/logs"

# One `python -m simulator ...` per command, chained with &&.
SCRIPT="set -o pipefail; cd $HOME/msml650_project/AnomalyPulse"
COMMAND=""
for arg in "$@" --; do
  if [ "$arg" = "--" ]; then
    [ -n "$COMMAND" ] && SCRIPT="$SCRIPT && $HOME/venv/bin/python -u -m simulator $COMMAND"
    COMMAND=""
  else
    COMMAND="$COMMAND$(printf '%q ' "$arg")"
  fi
done
SCRIPT="($SCRIPT) 2>&1 | tee -a $LOG; echo \"exit \${PIPESTATUS[0]}\" | tee -a $LOG"

tmux new-session -d -s "$SESSION" bash -c "$SCRIPT"
echo "started tmux session $SESSION; log: $LOG"
