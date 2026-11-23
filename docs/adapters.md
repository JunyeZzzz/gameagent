# 接入一个游戏

启动器通过 `python3 run.py --adapter 名称` 加载项目内的 `adapters/<名称>.py`。模块提供两个同步函数，`root` 是该局的状态目录。最小例子见 [sandbox.py](../adapters/sandbox.py)，带地图与交互物的例子见 [doorworld.py](../adapters/doorworld.py)。

## 把玩家可获得的信息交给模型

`observe(root)` 返回 JSON 可表示的快照。未就绪时，返回 `ready: false` 和等待原因 `reason`，有未决动作时仍应提供最新回执。就绪时，运行时使用以下字段。

| 字段 | 含义 |
| --- | --- |
| `session`、`revision` | 非空字符串。重开游戏更换会话；任何会影响选择、执行前提或目标的变化都要更新版本。 |
| `observed_at`、`valid_for` | 在游戏端实际采样的 Unix 时间及快照有效期，单位为秒。 |
| `goal`、`facts` | 当前目标与玩家可获得的现场信息，例如任务文字、位置、朝向、背包、地图。完整度由适配器负责。 |
| `actions` | 当前可选操作的列表。每项有唯一的非空字符串 `id`，可附说明或可预知的即时效果。目标或物品可以绑定到 ID。 |
| `routes` | 可选。按地标给出距离和抵达它的第一个原生动作。 |
| `last_result` | 最近的实际动作回执，需能按动作 ID 与游戏会话对应。 |

有地图时，由适配器决定能读取的地图范围及地标。MiniGrid 示例显式开启全图模式；其他游戏可从玩家能打开的地图或已探索区域构建路线。`runtime/navigation.py` 的 `route_options(start, neighbors, landmarks)` 支持有限图上的等代价有向边及多个抵达位置。可以把朝向纳入节点，或由游戏自己的导航服务给出首步。例如，`{"出口": {"distance": 3, "first_action": "forward"}}`。不可达用 `null`，已抵达用距离 0 和空首步。

## 提交与回执

`act(root, intent)` 接收 `id`、`session`、`revision`、`action`、`expires_at`，也可能附带模型选择的 `subgoal`。适配器在实际执行边界复核前提，按意图 ID 去重，回传相同 `id`、`session`，并提供下列状态和结果证据。

| 状态 | 运行时行为 |
| --- | --- |
| `completed`、`failed`、`cancelled`、`rejected` | 记录终态。完成的动作会触发立即重读。 |
| `accepted`、`running`、`unknown` | 保留待核实的意图。持续在 `last_result` 提供匹配的回执，直到确认终态。 |

运行时把待核实意图写在环境目录，重启后继续检查。执行端丢失回执时，该环境会等待核实。长时动作应由游戏端跟踪，适配器函数快速返回。切换游戏时使用独立的状态目录，同一状态或运行目录只允许一个工作进程。适配器是启动者选择的可信本地代码。

模型可调用 `game_observe` 刷新现场，通过 `game_schedule` 提交当轮的一个动作 ID。运行时在下发前复核会话、版本、有效期、候选项和路线首步。最新快照就绪时立即进入下一轮；暂停、未决、空闲或旧快照按 `GAMEAGENT_INTERVAL` 轮询。模型还可见当前游戏会话最近最多八个已完成动作及可辨认的状态变化。这段短期摘要保存在当前进程中。
