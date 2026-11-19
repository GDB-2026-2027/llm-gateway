#!/usr/bin/env python3
"""CLI minimale per gateway condiviso via GitHub. Solo stdlib + python-dotenv.

Gli alias possono essere lanciati da qualsiasi directory dopo 'source activate.sh'.
Nessun comando comunica a B2 tranne 'cache pull' e 'cache push'.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def fail(message: str, code: int = 2) -> None:
    print(f"ERRORE: {message}", file=sys.stderr)
    raise SystemExit(code)


def python_module(module: str, *arguments: str, env: dict[str, str] | None = None) -> None:
    os.chdir(ROOT)
    env = env if env is not None else os.environ.copy()
    os.execvpe(sys.executable, [sys.executable, "-m", module, *arguments], env)


def local_env_path(path: str) -> Path:
    p = Path(path).expanduser()
    return (p if p.is_absolute() else ROOT / p).resolve()


def run_start(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="start", description="Avvio e stato gateway")
    sub = parser.add_subparsers(dest="action", required=True)
    server = sub.add_parser("server", help="avvia, caricando di default azure.local.env")
    server.add_argument("--env", default="azure.local.env", help="percorso .env (default: azure.local.env)")
    server.add_argument("--no-env", action="store_true", help="non caricare file .env")
    sub.add_parser("status", help="HTTP GET /health, senza chiamate LLM")
    a = parser.parse_args(arguments)
    if a.action == "server":
        env = os.environ.copy()
        if not a.no_env:
            from dotenv import dotenv_values

            env_path = local_env_path(a.env)
            if env_path.is_file():
                for key, value in dotenv_values(env_path).items():
                    if value is not None:
                        # Le variabili esportate esplicitamente hanno priorita'.
                        env.setdefault(key, value)
                print(f"Credenziali caricate da {env_path.name} (valori non mostrati).", flush=True)
        # Google optional: proprio file locale, non in Git.
        google = ROOT / "google.local.env"
        if google.is_file():
            from dotenv import dotenv_values

            for key, value in dotenv_values(google).items():
                if value is not None:
                    env.setdefault(key, value)
        python_module("llm_gateway.server", env=env)
    else:
        try:
            with (ROOT / "gateway.toml").open("rb") as f:
                config = tomllib.load(f)
            server = config.get("server", {})
            host = server.get("host", "127.0.0.1")
            if host in ("0.0.0.0", "::", "[::]"):
                host = "127.0.0.1"
            port = int(server.get("port", 8000))
            with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3) as r:
                body = r.read(200).decode("utf-8", errors="replace")
                print(f"Gateway HTTP {r.status}: {body}")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            fail(f"gateway non raggiungibile: {exc}", 1)


def run_cache(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="cache", description="Sincronizzazione B2 esistente")
    parser.add_argument("action", choices=("status", "pull", "push"))
    parser.add_argument("--config", default="gateway.toml")
    a = parser.parse_args(arguments)
    cfg = str(local_env_path(a.config))
    python_module("llm_gateway.cache_sync", "--config", cfg, a.action)


def doctor() -> None:
    ok = True
    print(f"Gateway: {ROOT}")
    for name in ("gateway.toml", "models.toml", ".venv/bin/python", "tools/semops_cli.py"):
        exists = (ROOT / name).is_file()
        print(f"{'OK' if exists else 'ERRORE'} {name}")
        ok &= exists
    package = ROOT / "src" / "llm_gateway"
    if not package.is_dir():
        package = ROOT / "llm_gateway"
    exists = package.is_dir()
    print(f"{'OK' if exists else 'ERRORE'} package llm_gateway ({package.relative_to(ROOT) if exists else 'non trovato'})")
    ok &= exists
    config = {}
    try:
        with (ROOT / "gateway.toml").open("rb") as f:
            config = tomllib.load(f)
        section = config.get("cache_sync", {})
        if "student_id" in section:
            print("ERRORE: togli [cache_sync].student_id da gateway.toml e usa SEMOPS_STUDENT_ID in b2.local.env")
            ok = False
        pricing = config.get("pricing", {})
        price_file = pricing.get("prices_path")
        if price_file:
            price_path = (ROOT / str(price_file)).resolve()
            has_price = price_path.is_file()
            print(f"{'OK' if has_price else 'ERRORE'} listino prezzi: {price_file}")
            ok &= has_price
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"ERRORE gateway.toml: {exc}")
        ok = False
    model_file = ROOT / str(config.get("routing", {}).get("models_path", "models.toml"))
    try:
        with model_file.open("rb") as f:
            models = tomllib.load(f).get("models", {})
        print(f"OK rotte modelli: {len(models)}")
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"ERRORE models.toml: {exc}")
        ok = False
        models = {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        print("ERRORE python-dotenv non installato")
        ok = False
        dotenv_values = None
    if dotenv_values:
        azure = ROOT / "azure.local.env"
        b2 = ROOT / "b2.local.env"
        for file in (azure, b2):
            print(f"{'OK' if file.exists() else 'AVVISO'} {file.name} {'presente' if file.exists() else 'assente'}")
        b2_values = dotenv_values(b2) if b2.is_file() else {}
        for key in ("SEMOPS_STUDENT_ID", "B2_ACCESS_KEY_ID", "B2_SECRET_ACCESS_KEY"):
            print(f"{'OK' if b2_values.get(key) else 'AVVISO'} {key}: {'impostato' if b2_values.get(key) else 'mancante'}")
        azure_values = dotenv_values(azure) if azure.is_file() else {}
        for key in sorted({route.get("api_key_env") for route in models.values() if isinstance(route, dict)} - {None}):
            found = key in os.environ or azure_values.get(key)
            print(f"{'OK' if found else 'AVVISO'} {key}: {'disponibile' if found else 'mancante'}")
    if not ok:
        fail("controlli essenziali non superati", 1)
    print("Controlli essenziali PASS. Gli AVVISI indicano opzioni cloud non ancora configurate.")


def run_runs(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="runs", description="Validazione e generazione run file")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("check", help="controlla i run file, genera results.csv")
    sub.add_parser("build", help="crea una run da manifest.json e answer.json")
    known, remaining = parser.parse_known_args(arguments)
    if not (ROOT / "semops_check.py").is_file():
        fail("semops_check.py non trovato nella radice del gateway")
    env = os.environ.copy()
    os.chdir(ROOT)
    if known.action == "check":
        arguments = [".", "--results", "results.csv", *remaining]
    else:
        arguments = ["--build-run", "--repo-root", ".", "--gateway-config", "gateway.toml", *remaining]
    os.execvpe(sys.executable, [sys.executable, "semops_check.py", *arguments], env)


def main() -> None:
    if len(sys.argv) < 2:
        fail("uso: start server/status | cache pull/push/status | doctor | runs check/build")
    command = sys.argv[1]
    args = sys.argv[2:]
    if command == "start":
        run_start(args)
    elif command == "cache":
        run_cache(args)
    elif command == "doctor":
        if args:
            fail("doctor non accetta argomenti")
        doctor()
    elif command == "runs":
        run_runs(args)
    else:
        fail(f"comando sconosciuto: {command}")


if __name__ == "__main__":
    main()
