#!/bin/bash
# Test xAI Grok integration

echo "Testing xAI Grok integration..."
echo ""

# Test with POST to /chat/stream
timeout 15 curl -X POST http://localhost:9091/chat/stream \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "Hello! What AI model are you? Please respond in one sentence."}
    ],
    "provider": "xai",
    "model": "grok-4.3",
    "operator": "Brandon-DEV"
  }' 2>&1

echo ""
echo ""
echo "Test complete!"
