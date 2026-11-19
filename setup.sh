#!/usr/bin/env bash
# Repeatable gateway installation in the student's repository copy.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo 'ERROR: uv is missing. On macOS: brew install uv' >&2
  echo 'Alternative: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 2
fi
if [[ ! -f gateway.toml.example || ! -f models.toml.example ]]; then
  echo 'ERROR: setup.sh must be in the gateway root (with gateway.toml.example and models.toml.example).' >&2
  exit 2
fi
if [[ ! -d src/llm_gateway && ! -d llm_gateway ]]; then
  echo 'ERROR: llm_gateway package not found in src/ or the gateway root.' >&2
  exit 2
fi
if [[ ! -f pyproject.toml || ! -f uv.lock || ! -f tools/semops_cli.py ]]; then
  echo 'ERROR: pyproject.toml, uv.lock, or gateway command files are missing.' >&2
  exit 2
fi

# Create local configuration without replacing existing settings.
for name in gateway.toml models.toml; do
  if [[ ! -e "$name" ]]; then
    cp "$name.example" "$name"
    echo "Created $name from $name.example (edit it for your environment)."
  fi
done

PYTHON_VERSION="${SEMOPS_PYTHON_VERSION:-3.12}"
echo "Installing gateway from uv.lock (Python $PYTHON_VERSION) ..."
uv sync --locked --python "$PYTHON_VERSION"

# Create local credential files without replacing existing settings.
for name in azure.local.env b2.local.env google.local.env; do
  if [[ ! -e "$name" && -f "templates/$name.example" ]]; then
    cp "templates/$name.example" "$name"
    chmod 600 "$name"
    echo "Created $name (complete it locally; DO NOT commit it)."
  fi
done

chmod +x bin/start bin/cache bin/doctor bin/runs tools/semops_cli.py

echo
echo 'INSTALLATION COMPLETE.'
echo '1. Optional: configure cloud credentials and B2 only if you need them.'
echo '2. Run: source ./activate.sh'
echo '3. Run: doctor'
echo '4. Run: start server        (in the terminal that will stay open)'
echo '5. In a second terminal, run: source ./activate.sh; start status'
echo '6. Optional: cache status; cache pull; cache push (requires b2.local.env)'
echo 'Optional RDF bindings: uv sync --locked --group rdflib [--group oxigraph].'
