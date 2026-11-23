#!/usr/bin/env python3
"""Launch one local GameAgent worker. The selected adapter owns game I/O."""
import argparse
import fcntl
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parent
RUNTIME = PROJECT / "runtime"
sys.path.insert(0, str(RUNTIME))
from environment import adapter_for, inside, write_json
from persistent_gameagent import serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", default=os.getenv("GAMEAGENT_ADAPTER", "sandbox"))
    parser.add_argument("--once", action="store_true", help="Run one observation/decision cycle")
    args = parser.parse_args()
    state = inside(PROJECT, os.getenv("GAMEAGENT_ROOT", PROJECT / "state"))
    home = inside(PROJECT, os.getenv("GAMEAGENT_HOME", PROJECT / "data"))
    if state == home or state in home.parents or home in state.parents:
        parser.error("运行数据和环境状态目录必须分开")
    adapter = adapter_for(args.adapter)
    key = os.getenv("GAMEAGENT_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        parser.error("请设置 GAMEAGENT_API_KEY 或 DEEPSEEK_API_KEY")
    if not (RUNTIME / "node_modules/.bin/pi").exists():
        parser.error("请先在 runtime 目录运行 npm ci")
    for directory in (state, home):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Lock both resources before changing shared configuration or starting a model session.
    with (state / ".gameagent.lock").open("a") as world_lock, (home / "worker.lock").open("a") as worker_lock:
        for lock in (world_lock, worker_lock):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error("已有 GameAgent 使用此状态目录或会话目录")
        model = os.getenv("GAMEAGENT_MODEL", "deepseek-flash")
        config = home / "config"
        config.mkdir(mode=0o700, exist_ok=True)
        provider = {"baseUrl": os.getenv("GAMEAGENT_MODEL_URL", "https://api.deepseek.com"),
                    "api": "openai-completions", "apiKey": "$GAMEAGENT_API_KEY",
                    "models": [{"id": model, "name": model, "input": ["text"], "reasoning": False,
                                "contextWindow": 131072, "maxTokens": 1024}]}
        write_json(config / "models.json", {"providers": {"gameagent": provider}})
        env = dict(os.environ, GAMEAGENT_API_KEY=key, GAMEAGENT_HOME=str(home),
                   GAMEAGENT_ROOT=str(state), GAMEAGENT_ADAPTER=args.adapter,
                   GAMEAGENT_PYTHON=sys.executable, PI_CODING_AGENT_DIR=str(config),
                   GAMEAGENT_PROMPT_CLOCK=str(home / "round.json"),
                   GAMEAGENT_TRACE_DIR=str(home / "flow-trace"))
        return serve(home, adapter, model, env, args.once)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        sys.exit(str(error))
