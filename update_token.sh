#!/usr/bin/env bash

set -euo pipefail

ENV_FILE=".env"

usage() {
    cat <<EOF
Usage:
  $0 --token-name VAR_NAME --new-token TOKEN
  $0 -n VAR_NAME -t TOKEN

Examples:
  $0 --token-name OANDA_API_TOKEN --new-token "abc123"
  $0 -n OANDA_API_TOKEN -t "abc123"

Options:
  -n, --token-name   Environment variable name to update
  -t, --new-token    New token value
  -h, --help         Show this help
EOF
}

TOKEN_NAME=""
NEW_TOKEN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--token-name)
            TOKEN_NAME="$2"
            shift 2
            ;;
        -t|--new-token)
            NEW_TOKEN="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

if [[ -z "$TOKEN_NAME" || -z "$NEW_TOKEN" ]]; then
    echo "Error: Both token name and token value are required."
    usage
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: $ENV_FILE not found."
    exit 1
fi

if grep -q "^export ${TOKEN_NAME}=" "$ENV_FILE"; then
    sed -i "s|^export ${TOKEN_NAME}=.*|export ${TOKEN_NAME}=\"${NEW_TOKEN}\"|" "$ENV_FILE"
    echo "✅ Updated ${TOKEN_NAME}"
else
    echo "export ${TOKEN_NAME}=\"${NEW_TOKEN}\"" >> "$ENV_FILE"
    echo "✅ Added ${TOKEN_NAME}"
fi