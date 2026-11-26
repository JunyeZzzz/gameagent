"""Replay archived native actions and compare each transition with its recorded receipt."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

import gymnasium as gym
from minigrid.wrappers import FullyObsWrapper

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "runtime"))
from environment import adapter_for

DATA = PROJECT / "benchmarks" / "data"
TAXI_ACTIONS = ("south", "north", "east", "west", "pickup", "dropoff")


def check_episode(episode, steps, grid_adapter):
    name = episode["id"]
    if not steps or len(steps) != episode["native_steps"]:
        raise ValueError(f"{name}: missing or unexpected steps")
    taxi = episode["game"] == "Taxi-v4"
    if taxi:
        settings = {key: episode[key] for key in ("is_rainy", "rainy_probability",
                                                 "fickle_passenger", "fickle_probability") if key in episode}
        env = gym.make("Taxi-v4", **settings)
    else:
        env = FullyObsWrapper(gym.make(episode["game"]))
    try:
        env.reset(seed=episode["seed"])
        for index, record in enumerate(steps, 1):
            action = record["action"]
            primitive = TAXI_ACTIONS.index(action) if taxi else env.unwrapped.actions[action]
            obs, reward, terminated, truncated, _ = env.step(primitive)
            actual = dict(step=index, reward=float(reward), terminated=bool(terminated),
                          truncated=bool(truncated))
            if taxi:
                actual["state"] = int(obs)
            else:
                view = grid_adapter.view(obs, env)
                for field in ("map_rows", "xy", "facing", "inventory"):
                    actual[field] = view[field]
            for field, value in actual.items():
                if value != record[field]:
                    raise ValueError(f"{name} step {index} {field}: {value!r} != {record[field]!r}")
            if (terminated or truncated) and index != len(steps):
                raise ValueError(f"{name}: recorded actions continue past a terminal state")
        if terminated:
            outcome = "win" if reward > 0 else "loss"
        elif truncated:
            outcome = "truncated"
        elif len(steps) == episode.get("step_budget"):
            outcome = "step_cap"
        else:
            raise ValueError(f"{name}: unfinished recording")
        if outcome != episode["outcome"]:
            raise ValueError(f"{name}: {outcome} != {episode['outcome']}")
    finally:
        env.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", help="Replay a single ID, e.g. repeat/base-01")
    args = parser.parse_args()
    episodes = json.loads((DATA / "episodes.json").read_text(encoding="utf-8"))
    records = defaultdict(list)
    with (DATA / "native-steps.jsonl").open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            records[record["episode"]].append(record)
    ids = {e["id"] for e in episodes}
    if len(ids) != len(episodes) or ids != records.keys():
        raise ValueError("Episode index and recorded trajectories disagree")
    if args.episode:
        if args.episode not in ids:
            parser.error(f"Unknown episode: {args.episode}")
        episodes = [e for e in episodes if e["id"] == args.episode]
    grid_adapter = adapter_for("doorworld")
    total = 0
    for episode in episodes:
        steps = records[episode["id"]]
        check_episode(episode, steps, grid_adapter)
        total += len(steps)
        print(f"{episode['id']:<18} {len(steps):>2} steps  {episode['outcome']}")
    print(f"Verified {len(episodes)} episodes, {total} native steps.")


if __name__ == "__main__":
    main()
