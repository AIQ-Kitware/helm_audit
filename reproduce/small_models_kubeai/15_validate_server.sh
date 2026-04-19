#!/usr/bin/env bash
set -euo pipefail

KUBEAI_BASE_URL="${KUBEAI_BASE_URL:-http://127.0.0.1:8000/openai/v1}"

echo "Checking /models at $KUBEAI_BASE_URL"
curl -fsS "$KUBEAI_BASE_URL/models"
echo

echo "Checking Qwen chat endpoint"
curl -fsS "$KUBEAI_BASE_URL/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen2-5-7b-instruct-turbo-default",
    "messages": [{"role": "user", "content": "Reply with the word ready."}],
    "max_tokens": 16
  }'
echo

echo "Checking Vicuna completions endpoint"
curl -fsS "$KUBEAI_BASE_URL/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "vicuna-7b-v1-3-no-chat-template",
    "prompt": "Reply with the word ready.",
    "max_tokens": 16
  }'
echo
