# Sourced by setup.sh and install. Authentication remains in the user's terminal.
privileged_options=()

prepare_external_agent() {
  command -v pkttyagent >/dev/null || {
    echo 'pkttyagent is missing; install the Debian polkitd package first.' >&2
    return 1
  }
  echo 'In a SECOND terminal/SSH session on this machine, as the same user, run:'
  printf '\n  pkttyagent --process %s\n\n' "$$"
  echo 'Leave it running. Password prompts will appear in that second terminal.'
  read -r -p 'Once the agent is running, press Enter here to continue: ' _agent_ready
  privileged_options=(--disable-internal-agent)
}

run_privileged() {
  local result
  if pkexec "${privileged_options[@]}" "$@"; then
    return 0
  else
    result=$?
    echo 'Privileged step failed. For a polkit authentication/session error,' >&2
    echo 'rerun this script with --external-agent; see the README troubleshooting steps.' >&2
    return "$result"
  fi
}
