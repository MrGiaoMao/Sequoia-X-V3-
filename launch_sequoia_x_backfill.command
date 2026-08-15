#!/bin/zsh

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$PROJECT_DIR" || {
  echo "Failed to enter project directory:"
  echo "$PROJECT_DIR"
  printf "\nPress any key to close..."
  read -r -k 1
  exit 1
}

echo "Starting Sequoia-X initial backfill..."
echo "Project: $PROJECT_DIR"
echo "Mode: python main.py --backfill"
echo

if [ ! -f ".env" ]; then
  echo "Missing .env file."
  echo "Please create .env from .env.example and fill FEISHU_WEBHOOK_URL."
  printf "\nPress any key to close..."
  read -r -k 1
  exit 1
fi

if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "python3 was not found."
  printf "\nPress any key to close..."
  read -r -k 1
  exit 1
fi

"$PYTHON" main.py --backfill
STATUS=$?

echo
if [ "$STATUS" -eq 0 ]; then
  echo "Sequoia-X backfill finished successfully."
else
  echo "Sequoia-X backfill failed with exit code $STATUS."
fi

printf "\nPress any key to close..."
read -r -k 1
exit "$STATUS"
