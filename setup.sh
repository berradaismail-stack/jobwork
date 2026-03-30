#!/bin/bash
set -e

echo ""
echo "==================================="
echo "  Invoice Verifier — Setup"
echo "==================================="
echo ""

if ! command -v python3 &>/dev/null; then
    echo "  Python 3 not found."
    exit 1
fi
echo "  Python $(python3 --version) found"

if [ ! -d "venv" ]; then
    echo "  Creating virtual environment..."
    python3 -m venv venv
fi
echo "  Virtual environment ready"

echo "  Installing dependencies..."
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
echo "  Dependencies installed"

mkdir -p data uploads

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "  .env created — add your ANTHROPIC_API_KEY"
    echo "  File: $(pwd)/.env"
else
    echo "  .env already exists"
fi

echo ""
echo "==================================="
echo "  Setup complete!"
echo "  1. Add your API key to .env"
echo "  2. Run:  ./start.sh"
echo "  3. Open: http://localhost:8083"
echo "==================================="
echo ""
