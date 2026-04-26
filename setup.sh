#!/bin/bash
# ============================================================
# setup.sh - Automatic Setup Script
# ============================================================
# This script automates the entire setup process for the
# Blogger Automation Bot. Just run it once to get started!
#
# USAGE:
#   chmod +x setup.sh      (first time only, to make it executable)
#   ./setup.sh             (run the setup)
#   python main.py         (start the bot)
#
# ============================================================

echo ""
echo "============================================================"
echo "  🔐 BLOGGER AUTOMATION BOT - SETUP WIZARD"
echo "============================================================"
echo ""

# --------------------------------------------------
# 1) Check Python version
# --------------------------------------------------
echo "📌 Step 1: Checking Python version..."

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
REQUIRED_MAJOR=3
REQUIRED_MINOR=10

if [ -z "$PYTHON_VERSION" ]; then
    echo "  ❌ Python 3 is not installed!"
    echo "  → Download it from: https://www.python.org/downloads/"
    exit 1
fi

# Extract major and minor version numbers
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

echo "  ✅ Found Python $PYTHON_VERSION"

if [ "$PYTHON_MAJOR" -lt $REQUIRED_MAJOR ] || ([ "$PYTHON_MAJOR" -eq $REQUIRED_MAJOR ] && [ "$PYTHON_MINOR" -lt $REQUIRED_MINOR ]); then
    echo "  ⚠️  Warning: Python 3.10+ is recommended. You have $PYTHON_VERSION."
    echo "  → The bot may still work, but upgrading is recommended."
fi

echo ""

# --------------------------------------------------
# 2) Create a virtual environment
# --------------------------------------------------
echo "📌 Step 2: Creating a Python virtual environment..."

if [ -d "venv" ]; then
    echo "  ℹ️  Virtual environment already exists. Skipping creation."
else
    python3 -m venv venv
    if [ $? -eq 0 ]; then
        echo "  ✅ Virtual environment created in ./venv"
    else
        echo "  ❌ Failed to create virtual environment!"
        echo "  → Try running: python3 -m pip install --upgrade pip"
        exit 1
    fi
fi

echo ""

# --------------------------------------------------
# 3) Activate the virtual environment
# --------------------------------------------------
echo "📌 Step 3: Activating virtual environment..."

# Activate the venv
source venv/bin/activate

if [ $? -eq 0 ]; then
    echo "  ✅ Virtual environment activated!"
else
    echo "  ❌ Could not activate virtual environment!"
    exit 1
fi

echo ""

# --------------------------------------------------
# 4) Upgrade pip
# --------------------------------------------------
echo "📌 Step 4: Upgrading pip to latest version..."
pip install --upgrade pip -q
echo "  ✅ Pip upgraded!"

echo ""

# --------------------------------------------------
# 5) Install dependencies
# --------------------------------------------------
echo "📌 Step 5: Installing required Python packages..."
echo "  ⏳ This may take a minute on first run..."

pip install -r requirements.txt

if [ $? -eq 0 ]; then
    echo ""
    echo "  ✅ All packages installed successfully!"
else
    echo ""
    echo "  ❌ Failed to install some packages!"
    echo "  → Try running: pip install -r requirements.txt"
    exit 1
fi

echo ""

# --------------------------------------------------
# 6) Create .env file from template
# --------------------------------------------------
echo "📌 Step 6: Setting up configuration file..."

if [ -f ".env" ]; then
    echo "  ℹ️  .env file already exists. Skipping."
else
    cp .env.example .env
    echo "  ✅ Created .env file from template!"
fi

echo ""

# --------------------------------------------------
# 7) Create data directory
# --------------------------------------------------
echo "📌 Step 7: Creating data directories..."
mkdir -p data logs
echo "  ✅ Directories created!"

echo ""

# --------------------------------------------------
# 8) Final instructions
# --------------------------------------------------
echo "============================================================"
echo "  ✅ SETUP COMPLETE!"
echo "============================================================"
echo ""
echo "  📋 BEFORE RUNNING THE BOT, YOU NEED TO:"
echo ""
echo "  1. Edit the .env file and add your API keys:"
echo "     • Open .env in any text editor"
echo "     • Replace 'your_gemini_api_key_here' with your Gemini API key"
echo "     • Replace 'your_blog_id_here' with your Blogger Blog ID"
echo ""
echo "  2. Download client_secret.json from Google Cloud Console:"
echo "     • Go to: https://console.cloud.google.com/"
echo "     • Create a project (or select existing)"
echo "     • Enable the Blogger API v3"
echo "     • Create OAuth 2.0 credentials (Desktop App)"
echo "     • Download the JSON file and rename it to 'client_secret.json'"
echo "     • Place it in the project root folder (same folder as this script)"
echo ""
echo "  🚀 TO RUN THE BOT:"
echo "     source venv/bin/activate"
echo "     python main.py            (run once)"
echo "     python main.py --loop     (run continuously)"
echo ""
echo "============================================================"
echo ""
