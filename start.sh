#!/bin/bash
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    echo "  Run ./setup.sh first."
    exit 1
fi

if grep -q "your_anthropic_api_key_here" .env 2>/dev/null; then
    echo "  Warning: ANTHROPIC_API_KEY not set — screenshot extraction will fail."
fi

echo ""
echo "==================================="
echo "  Starting Invoice Verifier..."
echo "  Open http://localhost:8083"
echo "  Press Ctrl+C to stop"
echo "==================================="
echo ""

./venv/bin/python app.py
