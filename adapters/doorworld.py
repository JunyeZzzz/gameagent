"""MiniGrid supplies the mission, public full-map observation and primitive actions.
Assume deterministic reset and native transitions for a fixed seed and version.
Replaying short histories on each read costs time but keeps this one-user test isolated.
If a receipt disagrees with a native replay its integrity takes precedence over progress.
"""
import json
import os
import time

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import gymnasium as gym
from minigrid.core.constants import IDX_TO_OBJECT
from minigrid.wrappers import FullyObsWrapper
from environment import write_json
from navigation import route_options

GAME = "MiniGrid-DoorKey-5x5-v0"
DIRECTIONS = ["east", "south", "west", "north"]
GLYPHS = {"wall":"#", "empty":".", "key":"K", "goal":"G", "floor":".", "lava":"~"}
LEGEND = "# wall, . empty, ~ lava, K yellow key, L locked yellow door, D closed door, O open door, G goal, >v<^ player. Rows north to south, columns west to east; x,y start at zero."
DESCRIPTIONS = {"left":"Turn left 90 degrees", "right":"Turn right 90 degrees", "forward":"Move one cell ahead",
                "pickup":"Pick up the object in the cell ahead", "drop":"Drop the carried object into the cell ahead",
                "toggle":"Interact with the object or door in the cell ahead", "done":"Wait; this has no effect in DoorKey"}


def view(obs, env):
    image = obs["image"]
    width,height = image.shape[:2]
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            kind,_,value = map(int, image[x,y])
            name = IDX_TO_OBJECT[kind]
            if name == "agent":
                mark = ">v<^"[value]
            elif name == "door":
                mark = "ODL"[value]
            else:
                mark = GLYPHS[name]
            row.append(mark)
        rows.append("".join(row))
    game = env.unwrapped
    fx,fy = map(int,game.front_pos)
    inventory = f"{game.carrying.color} {game.carrying.type}" if game.carrying else None
    return {"map_rows":rows,"xy":list(map(int,game.agent_pos)), "facing":DIRECTIONS[obs["direction"]],
            "front_tile":rows[fy][fx],"inventory":inventory}


def restore(root):
    run = json.loads((root/"episode.json").read_text())
    env = FullyObsWrapper(gym.make(run["game"]))
    obs,_ = env.reset(seed=run["seed"])
    for item in run["steps"]:
        obs,reward,term,trunc,_ = env.step(env.unwrapped.actions[item["action"]])
        assert (view(obs,env),float(reward),term,trunc) == (item["after"],item["reward"],item["terminated"],item["truncated"])
    return run,env,obs


def map_options(facts, steps):
    # The sprite can cover its current tile. Use only previously visible map cells.
    rows = [list(row) for row in facts["map_rows"]]
    x,y = facts["xy"]
    underneath = "."
    for receipt in reversed(steps):
        symbol = receipt["before"]["map_rows"][y][x]
        if symbol not in ">v<^":
            underneath = symbol
            break
    rows[y][x] = underneath
    offsets = ((1,0),(0,1),(-1,0),(0,-1))
    def neighbors(pose):
        px,py,d = pose
        dx,dy = offsets[d]
        if rows[py+dy][px+dx] in ".OG":
            yield "forward", (px+dx,py+dy,d)
        yield "left", (px,py,(d-1)%4)
        yield "right",(px,py,(d+1)%4)
    landmarks = {}
    objects = {"K":"key","L":"locked door","D":"closed door","O":"open door","G":"goal"}
    for cy,row in enumerate(rows):
        for cx,tile in enumerate(row):
            if tile not in objects:
                continue
            if tile=="G":
                landmark=f"stand on {objects[tile]} ({cx},{cy})"
                poses=[(cx,cy,d) for d in range(4)]
            else:
                landmark=f"face {objects[tile]} ({cx},{cy})"
                poses=[(cx-dx,cy-dy,d) for d,(dx,dy) in enumerate(offsets)
                       if rows[cy-dy][cx-dx] in ".OG"]
            landmarks[landmark]=poses
    return route_options((x,y,DIRECTIONS.index(facts["facing"])),neighbors,landmarks)


def choices(env, facts):
    actions = [{"id":a.name,"description":DESCRIPTIONS[a.name]} for a in env.unwrapped.actions]
    x,y = facts["xy"]
    d = DIRECTIONS.index(facts["facing"])
    dx,dy = ((1,0),(0,1),(-1,0),(0,-1))[d]
    tile = facts["front_tile"]
    effects = {"left":{"facing_after":DIRECTIONS[(d-1)%4],"xy_after":[x,y]},
               "right":{"facing_after":DIRECTIONS[(d+1)%4],"xy_after":[x,y]}}
    outcome = {"#":"blocked","L":"blocked","D":"blocked","K":"blocked",
               "~":"episode ends in failure","G":"reach goal and finish"}.get(tile,"move")
    effects["forward"] = {"tile_ahead":tile, "outcome":outcome,
                          "xy_after":[x,y] if outcome=="blocked" else [x+dx,y+dy]}
    for action in actions:
        if action["id"] in effects:
            action["immediate_effect"] = effects[action["id"]]
    return actions


def observe(root):
    run,env,obs = restore(root)
    try:
        steps=run["steps"]
        last=steps[-1] if steps else None
        reason = "native_end" if last and (last["terminated"] or last["truncated"]) else "test_step_limit" if len(steps)>=run["limit"] else None
        facts = {**view(obs,env),"map_legend":LEGEND,"step":len(steps)}
        return {"ready":reason is None,"reason":reason,"session":run["name"],"revision":str(len(steps)),
                **({"routes":map_options(facts,steps)} if run["routes"] else {}),
                "observed_at":time.time(),"valid_for":90,"goal":obs["mission"],
                "facts":facts,
                "actions":choices(env,facts),
                "last_result":last}
    finally:
        env.close()


def act(root,intent):
    run,env,obs = restore(root)
    try:
        index = len(run["steps"])
        if intent["session"] != run["name"] or intent["revision"] != str(index):
            return {"id":intent["id"],"session":intent["session"],"status":"rejected","reason":"world_changed"}
        before = view(obs,env)
        obs,reward,term,trunc,_ = env.step(env.unwrapped.actions[intent["action"]])
        receipt = {"id":intent["id"],"session":run["name"],"status":"completed","action":intent["action"],
                   "saved_subgoal":intent.get("subgoal",""),"before":before,"after":view(obs,env),
                   "reward":float(reward),"terminated":bool(term),"truncated":bool(trunc)}
        run["steps"].append(receipt)
        write_json(root/"episode.json",run)
        return receipt
    finally:
        env.close()
