#!/usr/bin/env bash
tmux kill-session -t dsh 2>/dev/null && echo "dsh stopped" || echo "dsh not running"
