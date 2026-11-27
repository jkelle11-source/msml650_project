#!/bin/bash
# Drive the runner host from a laptop: every live run's traffic comes from the runner, not the laptop
# (Docs/DATASET.md, "The runner host"). The laptop only sends commands, through Session Manager.
#
#     bash drive.sh status                          the instance, the experiment lock, tmux sessions
#     bash drive.sh start                           start the instance and wait until it takes commands
#     bash drive.sh setup <commit or branch>        check the repo out there (setup.sh)
#     bash drive.sh run <session> <simulator arguments...> [-- <more arguments...>]
#                                                   start them in tmux there (in-tmux.sh), one after another
#     bash drive.sh log <session> [lines]           the end of that session's newest log (default 20 lines)
#     bash drive.sh abort <session>                 stop the run going in that session, and the ones chained
#                                                   after it, cleanly; never press Ctrl-C in its tmux session
#     bash drive.sh stop                            stop the instance; refuses while a run is going
#
# Stop it after each session. If it is left on, the idle stop (idle-stop.yaml) stops it an hour after
# its traffic ends; `start` brings it back with the same instance id.
#
#     bash drive.sh run spike start-incident --type traffic-spike --intensity 0.7 --band 6 8 \
#         --duration 300 --baseline 360 --created-by "$USER"
#
# Needs the AWS CLI and a profile with AnomalyPulseDeveloper (default `dev`; set AWS_PROFILE to change it).
# DRY_RUN=1 prints the command the runner would run instead of sending it.
#
# Why `abort` and not Ctrl-C: in the tmux session, Ctrl-C reaches the whole `simulator | tee` pipeline, so the
# log's tee stops too. The simulator's wind-down then fails on its first print, before it releases the
# experiment lock, and the next run is refused until 5 minutes after the aborted run's planned end
# (2026-11-26, M10-1). `abort` interrupts the simulator alone: it cancels its faults, records the run as
# ABORTED, releases the lock, and the log shows it.
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-dev}" AWS_REGION="${AWS_REGION:-us-east-2}"
CHECKOUT='~/msml650_project/AnomalyPulse/infra/runner'

usage() { sed -n '5,20p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

# Run on the runner by `abort`, as `bash -c "$ABORT" abort <session>`. Every process in a tmux pane has the
# pane's terminal, so "on that terminal, with a command line starting with the venv's python" is the
# simulator alone: not its `bash -c` parents (whose command lines start with bash) or tee. A chain stops
# with it, since the next command runs only after a success. (`read`, not `$(cat <<EOF)`: macOS's bash 3.2
# misreads the case pattern's `)` inside `$( )`.)
IFS= read -r -d '' ABORT <<'EOF' || true
s="$1"
tmux has-session -t "=$s" 2>/dev/null || { echo "no tmux session '$s' on the runner" >&2; exit 1; }
tty=$(tmux display-message -p -t "=$s:" '#{pane_tty}')
tty=${tty#/dev/}
[ -n "$tty" ] || { echo "can't find the terminal of tmux session '$s'" >&2; exit 1; }
sim="$HOME/venv/bin/python -u -m simulator "
simulator_pids() {  # the processes on that terminal whose command line starts with $sim
  ps -ww -o pid= -o command= -t "$tty" 2>/dev/null | while read -r pid cmd; do   # gone once the pane closes
    case "$cmd" in "$sim"*) echo "$pid" ;; esac
  done
}
pids=$(simulator_pids)
[ -n "$pids" ] || { echo "nothing to abort: no simulator command is running in '$s'" >&2; exit 1; }
kill -INT $pids
for _ in $(seq 1 90); do [ -n "$(simulator_pids)" ] || break; sleep 1; done
if [ -n "$(simulator_pids)" ]; then
  echo "warning: the simulator in '$s' is still winding down after 90 s; check 'drive.sh log $s'" >&2
fi
sleep 1
tail -n 6 "$(ls -t "$HOME/logs/$s"-*.log | head -1)"
EOF

instance_id() {
  aws cloudformation describe-stacks --stack-name anomalypulse-runner \
    --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text
}

state() { aws ec2 describe-instances --instance-ids "$1" --query 'Reservations[0].Instances[0].State.Name' --output text; }

lock_holder() {
  aws dynamodb get-item --table-name anomalypulse-experiments --key '{"pk":{"S":"LOCK"}}' \
    --query 'Item.[experiment_id.S, held_by.S, expires_at.N]' --output text
}

# Run one shell command on the runner as ec2-user and print what it printed. Exits with its status.
remote() {
  local id="$1" command="$2" wait="${3:-300}"
  local wrapped="runuser -l ec2-user -c $(printf '%q' "$command")"
  if [ -n "${DRY_RUN:-}" ]; then
    echo "$wrapped"
    return 0
  fi
  local params cid status
  params=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": [sys.argv[2]]}))' \
           "$wrapped" "$wait")
  cid=$(aws ssm send-command --instance-ids "$id" --document-name AWS-RunShellScript --parameters "$params" \
        --query Command.CommandId --output text)
  for _ in $(seq 1 $((wait / 2 + 15))); do
    status=$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$id" --query Status --output text 2>/dev/null \
             || echo Pending)
    case "$status" in Pending|InProgress|Delayed) sleep 2 ;; *) break ;; esac
  done
  local out err
  out=$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$id" --query StandardOutputContent --output text)
  err=$(aws ssm get-command-invocation --command-id "$cid" --instance-id "$id" --query StandardErrorContent --output text)
  [ -z "$out" ] || echo "$out"
  [ -z "$err" ] || echo "$err" >&2
  [ "$status" = Success ]
}

need_running() {
  local s
  [ -z "${DRY_RUN:-}" ] && s=$(state "$1") || s=running
  [ "$s" = running ] || { echo "the runner is $s: run 'drive.sh start' first" >&2; exit 1; }
}

[ $# -ge 1 ] || usage
ID=$(instance_id)
case "$1" in
  status)
    S=$(state "$ID")
    echo "runner:  $ID $S"
    echo "lock:    $(lock_holder | sed 's/^None.*/free/')"
    if [ "$S" = running ]; then
      echo "tmux:    $(remote "$ID" 'tmux ls 2>/dev/null || echo none' 60 | tr '\n' ' ')"
    fi
    ;;
  start)
    [ -z "${DRY_RUN:-}" ] || { echo "would start $ID"; exit 0; }
    aws ec2 start-instances --instance-ids "$ID" --query 'StartingInstances[0].CurrentState.Name' --output text
    aws ec2 wait instance-running --instance-ids "$ID"
    for _ in $(seq 1 30); do
      ping=$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$ID" \
             --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)
      [ "$ping" = Online ] && break
      sleep 10
    done
    [ "$ping" = Online ] || { echo "the runner is running but Session Manager can't reach it yet; retry 'status'" >&2; exit 1; }
    echo "running, and taking commands"
    ;;
  setup)
    [ $# -eq 2 ] || usage
    need_running "$ID"
    remote "$ID" "bash $CHECKOUT/setup.sh $(printf '%q' "$2")" 600
    ;;
  run)
    [ $# -ge 3 ] || usage
    need_running "$ID"
    session="$2"
    shift 2
    args=""
    for a in "$@"; do args="$args $(printf '%q' "$a")"; done
    remote "$ID" "bash $CHECKOUT/in-tmux.sh $(printf '%q' "$session")$args && sleep 15 && tail -n 6 \$(ls -t ~/logs/$(printf '%q' "$session")-*.log | head -1)" 120
    ;;
  log)
    [ $# -ge 2 ] || usage
    need_running "$ID"
    remote "$ID" "tail -n ${3:-20} \$(ls -t ~/logs/$(printf '%q' "$2")-*.log | head -1)" 60
    ;;
  abort)
    [ $# -eq 2 ] || usage
    need_running "$ID"
    remote "$ID" "bash -c $(printf '%q' "$ABORT") abort $(printf '%q' "$2")" 150
    [ -n "${DRY_RUN:-}" ] || echo "lock:    $(lock_holder | sed 's/^None.*/free/')"
    ;;
  stop)
    [ -z "${DRY_RUN:-}" ] || { echo "would stop $ID, if no run is going"; exit 0; }
    holder=$(lock_holder)
    if [ "$(state "$ID")" = running ]; then
      sessions=$(remote "$ID" 'tmux ls 2>/dev/null || true' 60)
      if [ -n "$sessions" ]; then
        echo "a run is still going on the runner ($sessions); wait for it, or stop it there first" >&2
        exit 1
      fi
    fi
    case "$holder" in None*|"") ;; *) echo "note: the experiment lock is held ($holder)" >&2 ;; esac
    aws ec2 stop-instances --instance-ids "$ID" --query 'StoppingInstances[0].CurrentState.Name' --output text
    aws ec2 wait instance-stopped --instance-ids "$ID"
    echo "stopped"
    ;;
  *) usage ;;
esac
