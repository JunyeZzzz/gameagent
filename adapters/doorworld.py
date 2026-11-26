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
from minigrid.core.constants import IDX_TO_COLOR, IDX_TO_OBJECT, OBJECT_TO_IDX
from minigrid.envs.dynamicobstacles import DynamicObstaclesEnv
from minigrid.wrappers import FullyObsWrapper
from environment import write_json
from navigation import route_options

GAME = "MiniGrid-DoorKey-5x5-v0"
DIRECTIONS = ["east", "south", "west", "north"]
OFFSETS = ((1, 0), (0, 1), (-1, 0), (0, -1))
GLYPHS = {"wall":"#", "empty":".", "key":"K", "goal":"G", "floor":".", "lava":"~", "ball":"b", "box":"B", "unseen":"?"}
PICKUP_TILES = "KbB"
PLACE_AND_PICK_UP = "place_and_pick_up:"
UNLOCK_DOOR = "unlock_door:"
MAX_UNLOCK_INPUTS = 32
DOOR_STATES = ("open", "closed", "locked")
LANDMARKS = {"K":"key", "b":"ball", "B":"box", "L":"locked door", "D":"closed door", "O":"open door", "G":"goal"}
LEGEND = "# wall, . floor, ~ lava, K key, b ball, B box, L locked door, D closed door, O open door, G goal, ? unseen, >v<^ player. Rows north to south, columns west to east; x,y start at zero."
DESCRIPTIONS = {"left":"Turn left 90 degrees", "right":"Turn right 90 degrees", "forward":"Move one cell ahead",
                "pickup":"Pick up the object in the cell ahead with empty hands", "drop":"Drop the carried object into an empty cell ahead",
                "toggle":"Interact with the object or door in the cell ahead", "done":"Take no action this turn"}


def view(obs, env, *, include_objects=False):
    image = obs["image"]
    width,height = image.shape[:2]
    rows = []
    objects = []
    for y in range(height):
        row = []
        for x in range(width):
            kind,color,value = map(int, image[x,y])
            name = IDX_TO_OBJECT[kind]
            if name == "agent":
                mark = ">v<^"[value]
            elif name == "door":
                mark = "ODL"[value]
            else:
                mark = GLYPHS[name]
            if include_objects and name in ("door", "key", "box", "ball"):
                item = dict(type=name, color=IDX_TO_COLOR[color], xy=[x, y])
                if name == "door":
                    item["state"] = DOOR_STATES[value]
                objects.append(item)
            row.append(mark)
        rows.append("".join(row))
    game = env.unwrapped
    fx,fy = map(int,game.front_pos)
    inventory = f"{game.carrying.color} {game.carrying.type}" if game.carrying else None
    return {"map_rows":rows,"xy":list(map(int,game.agent_pos)), "facing":DIRECTIONS[obs["direction"]],
            "front_tile":rows[fy][fx] if 0 <= fx < width and 0 <= fy < height else "#", "inventory":inventory,
            **({"objects": objects} if include_objects else {})}


def restore(root):
    run = json.loads((root/"episode.json").read_text())
    env = FullyObsWrapper(gym.make(run["game"]))
    obs,_ = env.reset(seed=run["seed"])
    for item in run["steps"]:
        obs,reward,term,trunc,_ = env.step(env.unwrapped.actions[item["action"]])
        assert (view(obs,env),float(reward),term,trunc) == (item["after"],item["reward"],item["terminated"],item["truncated"])
    return run,env,obs


def terrain_rows(facts, steps):
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
    return tuple("".join(row) for row in rows)


def walkable(rows, x, y, tiles):
    return 0 <= y < len(rows) and 0 <= x < len(rows[y]) and rows[y][x] in tiles


def facing_poses(rows, x, y, tiles):
    return [(x-dx, y-dy, d) for d, (dx, dy) in enumerate(OFFSETS) if walkable(rows, x-dx, y-dy, tiles)]


def neighbors(rows, pose, tiles):
    x, y, direction = pose
    dx, dy = OFFSETS[direction]
    if walkable(rows, x+dx, y+dy, tiles):
        yield "forward", (x+dx, y+dy, direction)
    yield "left", (x, y, (direction-1)%4)
    yield "right", (x, y, (direction+1)%4)


def map_options(facts, rows, image):
    x, y = facts["xy"]
    landmarks = {}
    colors = {tuple(obj["xy"]): obj["color"] for obj in facts.get("objects", [])}
    needs_space = facts["inventory"] is not None and facts["front_tile"] in PICKUP_TILES
    drop_poses = []
    for cy,row in enumerate(rows):
        for cx,tile in enumerate(row):
            if needs_space and tile == "." and int(image[cx, cy, 0]) == OBJECT_TO_IDX["empty"]:
                drop_poses.extend(facing_poses(rows, cx, cy, ".OG"))
            if tile not in LANDMARKS:
                continue
            if tile=="G":
                landmark=f"stand on goal ({cx},{cy})"
                poses=[(cx,cy,d) for d in range(4)]
            else:
                color = colors.get((cx, cy))
                label = f"{LANDMARKS[tile]}, {color}" if color else LANDMARKS[tile]
                landmark=f"face {label} ({cx},{cy})"
                poses = facing_poses(rows, cx, cy, ".OG")
            landmarks[landmark]=poses
    if drop_poses:
        landmarks["face an empty cell to drop the carried object"] = drop_poses
    return route_options((x,y,DIRECTIONS.index(facts["facing"])),
                         lambda pose: neighbors(rows, pose, ".OG"), landmarks)


def native_actions(env):
    return {a.name: a for a in env.unwrapped.actions if env.action_space.contains(a.value)}


def choices(env, facts, image, mission):
    actions = [{"id":name,"description":DESCRIPTIONS[name]} for name in native_actions(env)]
    x,y = facts["xy"]
    d = DIRECTIONS.index(facts["facing"])
    dx,dy = OFFSETS[d]
    tile = facts["front_tile"]
    ahead = [x+dx, y+dy]
    effects = {"left":{"facing_after":DIRECTIONS[(d-1)%4],"xy_after":[x,y]},
               "right":{"facing_after":DIRECTIONS[(d+1)%4],"xy_after":[x,y]}}
    outcome = ("blocked" if tile in "#LDKbB" else {"~":"episode ends in failure","G":"reach goal and finish",
                                                    "?":"unknown"}.get(tile,"move"))
    effects["forward"] = {"tile_ahead":tile, "outcome":outcome}
    if outcome != "unknown":
        effects["forward"]["xy_after"] = [x, y] if outcome == "blocked" else ahead
    if facts.get("obstacles_move_each_action"):
        if tile in "#b":
            effects["forward"] = dict(tile_ahead=tile, outcome="episode ends in failure")
        elif tile == ".":
            effects["forward"] = dict(tile_ahead=tile, outcome="move if the cell stays clear after obstacles move")
    item = next((item for item in facts["objects"] if item["xy"] == ahead), None)
    carried = facts["inventory"]
    if carried:
        effects["pickup"] = dict(outcome="blocked", reason=f"Hands already hold {carried}. Drop it into an empty cell first.")
    elif tile in PICKUP_TILES and item:
        effects["pickup"] = dict(outcome="pick up object", inventory_after=f"{item['color']} {item['type']}")
    if tile != "?":
        if not carried:
            effects["drop"] = dict(outcome="blocked", reason="Hands are empty.")
        elif tile == "." and int(image[ahead[0], ahead[1], 0]) == OBJECT_TO_IDX["empty"]:
            effects["drop"] = dict(outcome="place carried object", position=ahead, inventory_after=None)
        else:
            effects["drop"] = dict(outcome="blocked", reason="The cell ahead is occupied.")
    if item and item["type"] == "door":
        key = f"{item['color']} key"
        if item["state"] == "locked" and carried != key:
            effects["toggle"] = dict(outcome="blocked", reason=f"Opening this door requires holding the {key}.")
        else:
            effects["toggle"] = dict(outcome="change door state", door_after="closed" if item["state"] == "open" else "open")
    elif tile == "B":
        effects["toggle"] = dict(outcome="open box and remove the box from the cell", inventory_after=carried)
        mission_boxes = [obj for obj in facts["objects"] if obj["type"] == "box"
                         and mission == f'pick up the {obj["color"]} box']
        if mission_boxes == [item]:
            effects["toggle"]["mission_outcome"] = "打开会永久从当前关卡移除任务指定的箱子，之后无法再拾取这个箱子。"
    for action in actions:
        if action["id"] in effects:
            action["immediate_effect"] = effects[action["id"]]
    return actions


def pickup_plan(run, env, facts, image):
    carried = facts["inventory"]
    if not carried or facts["front_tile"] not in PICKUP_TILES:
        return None
    x, y = facts["xy"]
    facing = DIRECTIONS.index(facts["facing"])
    dx, dy = OFFSETS[facing]
    target = [x + dx, y + dy]
    item = next((item for item in facts["objects"] if item["xy"] == target), None)
    if item is None:
        return None
    pickup = f'{item["color"]} {item["type"]}'
    game = env.unwrapped
    budget = min(run["limit"] - len(run["steps"]), game.max_steps - game.step_count)
    width, height = image.shape[:2]
    for turn, outbound, inbound in ((1, ("right",), ("left",)), (-1, ("left",), ("right",)),
                                    (2, ("right", "right"), ("left", "left"))):
        steps = (*outbound, "drop", *inbound, "pickup")
        dx, dy = OFFSETS[(facing + turn) % 4]
        drop = [x + dx, y + dy]
        px, py = drop
        if (len(steps) > budget or not (0 <= px < width and 0 <= py < height)
                or int(image[px, py, 0]) != OBJECT_TO_IDX["empty"]):
            continue
        identity = PLACE_AND_PICK_UP + json.dumps([carried, drop, pickup, target], separators=(",", ":"))
        action = {"id": identity, "numbered": True,
                  "description": f"把手里的 {carried} 放到空格 {drop}，然后拾取面前的 {pickup} {target}。",
                  "immediate_effect": {"inventory_after": pickup, "drop_xy": drop, "pickup_xy": target}}
        return steps, action
    return None


def change_tile(rows, tile, mark):
    x, y = tile
    row = rows[y]
    return (*rows[:y], row[:x] + mark + row[x+1:], *rows[y+1:])


def path_to_face(rows, start, target):
    return route_options(start, lambda pose: neighbors(rows, pose, ".O"),
                         {"target": facing_poses(rows, *target, ".O")}, with_path=True)["target"]


def route_with_key(rows, pose, door, key):
    prefix = ()
    if key is not None:
        path = path_to_face(rows, pose, key)
        if path is None:
            return None
        prefix = (*path["steps"], "pickup")
        pose = path["arrival"]
        rows = change_tile(rows, key, ".")
    approach = path_to_face(rows, pose, door)
    return (*prefix, *approach["steps"], "toggle") if approach else None


def adjacent_placements(rows, pose, empty):
    x, y, facing = pose
    for delta, turns in ((1, ("right",)), (-1, ("left",)), (0, ()), (2, ("right", "right"))):
        direction = (facing + delta) % 4
        dx, dy = OFFSETS[direction]
        slot = (x+dx, y+dy)
        if slot in empty and walkable(rows, *slot, "."):
            yield slot, (x, y, direction), (*turns, "drop")


def approaches_to_ball(rows, pose, ball):
    if ball is None:
        return [(pose, ())]
    poses = facing_poses(rows, *ball, ".O")
    routes = route_options(pose, lambda node: neighbors(rows, node, ".O"),
                           {arrival: [arrival] for arrival in poses}, with_path=True)
    return [(route["arrival"], (*route["steps"], "pickup")) for route in routes.values() if route]


def placements_after_pickup(rows, pose, empty):
    yield from adjacent_placements(rows, pose, empty)
    x, y, direction = pose
    dx, dy = OFFSETS[direction]
    if walkable(rows, x+dx, y+dy, ".O"):
        for slot, facing, turns in adjacent_placements(rows, (x+dx, y+dy, direction), empty):
            yield slot, facing, ("forward", *turns)


def unlock_plan(run, env, facts, image, rows):
    locked = [obj for obj in facts["objects"] if obj.get("state") == "locked"]
    if len(locked) != 1:
        return None
    door = locked[0]
    door_xy = tuple(door["xy"])
    color = door["color"]
    carried = facts["inventory"]
    key_name = f"{color} key"
    keys = [tuple(obj["xy"]) for obj in facts["objects"] if obj["type"] == "key" and obj["color"] == color]
    if carried != key_name and len(keys) != 1:
        return None
    key = None if carried == key_name else keys[0]
    pose = (*facts["xy"], DIRECTIONS.index(facts["facing"]))
    game = env.unwrapped
    budget = min(MAX_UNLOCK_INPUTS, run["limit"] - len(run["steps"]), game.max_steps - game.step_count)
    route = route_with_key(rows, pose, door_xy, key) if carried in (None, key_name) else None
    moved = None
    key_on_floor = key
    if route is None:
        balls = [obj for obj in facts["objects"] if obj["type"] == "ball"]
        if carried and carried.endswith(" ball"):
            ball_color, ball_xy = carried.removesuffix(" ball"), None
        elif len(balls) == 1 and carried in (None, key_name):
            ball_color, ball_xy = balls[0]["color"], tuple(balls[0]["xy"])
        else:
            return None
        width, height = image.shape[:2]
        empty = {(x, y) for x in range(width) for y in range(height)
                 if int(image[x, y, 0]) == OBJECT_TO_IDX["empty"]}
        starts = [(rows, pose, key, ())]
        if carried == key_name:
            starts = [(change_tile(rows, slot, "K"), head, slot, turns)
                      for slot, head, turns in adjacent_placements(rows, pose, empty)]
        for layout, head, position_of_key, prefix in starts:
            cleared = change_tile(layout, ball_xy, ".") if ball_xy is not None else layout
            for arrival, fetch in approaches_to_ball(layout, head, ball_xy):
                for slot, facing, place in placements_after_pickup(cleared, arrival, empty):
                    remainder = route_with_key(change_tile(cleared, slot, "b"), facing, door_xy, position_of_key)
                    if remainder is None:
                        continue
                    candidate = (*prefix, *fetch, *place, *remainder)
                    if route is None or len(candidate) < len(route):
                        route = candidate
                        moved = dict(type="ball", color=ball_color, xy=list(slot))
                        key_on_floor = position_of_key
    if route is None or len(route) > budget:
        return None
    opened = dict(door, state="open")
    identity = UNLOCK_DOOR + json.dumps([door_xy, color, key_on_floor, moved], separators=(",", ":"))
    description = f"用 {key_name} 打开锁门 {door['xy']}。"
    if moved:
        description += f"途中把 {moved['color']} ball 放到空格 {moved['xy']}，再带上钥匙开门。"
    if carried == key_name and key_on_floor is not None:
        description += f"腾手时暂放钥匙于 {list(key_on_floor)}，结束时取回。"
    effect = {"door": opened, "inventory_after": key_name, "input_count": len(route)}
    if moved:
        effect["ball"] = moved
    return route, dict(id=identity, numbered=True, description=description, immediate_effect=effect)


def latest_receipt(steps):
    if not steps:
        return None
    last = steps[-1]
    compound = last.get("compound")
    if compound is None:
        return last
    native = steps[-compound["count"]:]
    return dict(last, action=compound["action"], before=native[0]["before"],
                native_actions=[step["action"] for step in native])


def observe(root):
    run,env,obs = restore(root)
    try:
        steps=run["steps"]
        last=steps[-1] if steps else None
        revision = str(len(steps))
        uncommitted = run.get("uncommitted_receipt") or {}
        last_result = (uncommitted if (uncommitted.get("session"), uncommitted.get("revision"))
                       == (run["name"], revision) else latest_receipt(steps))
        reason = "native_end" if last and (last["terminated"] or last["truncated"]) else "test_step_limit" if len(steps)>=run["limit"] else None
        facts = {**view(obs,env,include_objects=True),"map_legend":LEGEND,"step":len(steps)}
        if isinstance(env.unwrapped, DynamicObstaclesEnv):
            facts["obstacles_move_each_action"] = True
        actions = choices(env, facts, obs["image"], obs["mission"])
        routes = {}
        if run["routes"]:
            rows = terrain_rows(facts, steps)
            routes["routes"] = map_options(facts, rows, obs["image"])
            door_plan = unlock_plan(run, env, facts, obs["image"], rows) if reason is None else None
            if door_plan:
                actions.insert(0, door_plan[1])
        pickup = pickup_plan(run, env, facts, obs["image"]) if reason is None else None
        if pickup:
            actions.insert(0, pickup[1])
        return {"ready":reason is None,"reason":reason,"session":run["name"],"revision":revision,
                **routes,
                "observed_at":time.time(),"valid_for":90,"goal":obs["mission"],
                "facts":facts,
                "actions":actions,
                "last_result":last_result}
    finally:
        env.close()


def reconcile_pending(root, intent):
    # Under the runtime lock an unchanged, complete journal proves no durable native step occurred.
    path = root / "episode.json"
    run = json.loads(path.read_text())
    if run["name"] != intent["session"] or str(len(run["steps"])) != intent["revision"]:
        return None
    if any(step["id"] == intent["id"] for step in run["steps"]):
        return None
    proof = dict(id=intent["id"], session=intent["session"], revision=intent["revision"],
                 action=intent["action"], status="failed", reason="action_not_committed")
    run["uncommitted_receipt"] = proof
    write_json(path, run)
    return proof


def act(root,intent):
    run,env,obs = restore(root)
    try:
        index = len(run["steps"])
        action = intent["action"]
        rejection = dict(id=intent["id"], session=intent["session"], action=action, status="rejected")
        if intent["session"] != run["name"] or intent["revision"] != str(index):
            return dict(rejection, reason="world_changed")
        plan = None
        sequence = (action,)
        if action.startswith((PLACE_AND_PICK_UP, UNLOCK_DOOR)):
            facts = view(obs, env, include_objects=True)
            if action.startswith(UNLOCK_DOOR):
                rows = terrain_rows(facts, run["steps"])
                plan = unlock_plan(run, env, facts, obs["image"], rows) if run["routes"] else None
            else:
                plan = pickup_plan(run, env, facts, obs["image"])
            if plan is None or plan[1]["id"] != action:
                return dict(rejection, reason="compound_option_changed")
            sequence = plan[0]
        available = native_actions(env)
        if any(primitive not in available for primitive in sequence):
            return dict(rejection, reason="action_unavailable")
        before = view(obs, env)
        for offset, primitive in enumerate(sequence, 1):
            obs, reward, term, trunc, _ = env.step(available[primitive])
            after = view(obs, env)
            last = offset == len(sequence)
            action_id = intent["id"] if last else f'{intent["id"]}:{offset}'
            run["steps"].append(dict(id=action_id, session=run["name"], status="completed", action=primitive,
                                     saved_subgoal=intent.get("subgoal", ""), before=before, after=after,
                                     reward=float(reward), terminated=bool(term), truncated=bool(trunc)))
            if not last and (term or trunc):
                return dict(rejection, reason="compound_interrupted")
            before = after
        if plan:
            effect = plan[1]["immediate_effect"]
            if after["inventory"] != effect["inventory_after"]:
                return dict(rejection, reason="compound_result_mismatch")
            if "door" in effect:
                objects = view(obs, env, include_objects=True)["objects"]
                if effect["door"] not in objects or ("ball" in effect and effect["ball"] not in objects):
                    return dict(rejection, reason="unlock_not_confirmed")
            elif after["front_tile"] != ".":
                return dict(rejection, reason="pickup_not_confirmed")
            else:
                px, py = effect["drop_xy"]
                kind, color, _ = map(int, obs["image"][px, py])
                placed = f"{IDX_TO_COLOR[color]} {IDX_TO_OBJECT[kind]}"
                if placed != run["steps"][index]["before"]["inventory"]:
                    return dict(rejection, reason="pickup_not_confirmed")
            run["steps"][-1]["compound"] = dict(action=action, count=len(sequence))
        # The simulator restores from this journal. One write makes every native step visible together.
        run.pop("uncommitted_receipt", None)
        write_json(root/"episode.json",run)
        return latest_receipt(run["steps"])
    finally:
        env.close()
