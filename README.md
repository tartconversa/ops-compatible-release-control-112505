# 兼容窗口内的灰度发布与回撤

解析服务发布演练，流量范围由 region 和 client_cap 共同定义。观察是来源系统已聚合的计数，不是发布结论。构建编号、队列格式编号和路由代次是不同的标识。

## 在本地查看资料

运行环境使用 Python 3.11.2 和标准库。启动资料浏览器无需安装额外包。

```sh
python materials.py
python materials.py builds
python -m unittest discover -s tests -v
```

资料浏览器也接受下方的其他资料名。JSONL 使用 UTF-8 编码，每行是一个对象。表内的顺序对应来源记录，事件时间与接收时间的区别以资料说明为准。各入口可以在无外网的环境中运行。

资料字段和业务口径见[演练资料格式](materials/README.md)。

## 本地发布控制台

`release_console.py` 是仅使用 Python 标准库的本地控制台；`console.py` 是等价入口。资料 JSONL 始终只读，操作员请求、路由回执、实例回执、领取记录和手工冻结写入本地状态文件（默认 `./release-console-state.json`）。

```sh
# 总览内置演练资料；可加 --at 43/49/53/99 回放不同到达时间
python release_console.py report
python release_console.py report --at 49

# 单次发布的完整判定；--json 可输出审计档案
python release_console.py show rel-1
python release_console.py audit rel-3 > rel-3-audit.json
python release_console.py actions
python release_console.py jobs --waiting

# 继续等待晚到样本：把逻辑观察时间推进到指定毫秒
python release_console.py wait --to 53

# 处理下一版清单：整个目录保持 builds/policy/observations/measurements.jsonl 即可
python release_console.py --materials-dir /path/to/next-materials report
```

也可以把选项放在子命令后：`python release_console.py show rel-4 --json`。

### 控制台的安全口径

- **版本先核验**：`propose --build ...` 指定目标构建；只有实例回执显示目标实例确实加载该构建，才允许发出路由。清单中的 `ready_slots` 只作为声明容量。
- **证据按实际生效归属**：样本必须匹配 `region`、`client_cap`、`route_seq`，且计数区间位于对应 `route_applied` 实际生效起点到下一代回执之间。请求时间不会被当成生效时间，生效前或跨代次样本会列入 `excluded_measurements`。
- **依赖故障单独处理**：`dep=unknown/impaired` 不计入健康样本。候选和对照在重叠区间同时为 `impaired` 时判定为 `shared_dependency_incident`，动作是暂停；既不算候选独有回归，也不算健康证明。
- **证据只支持已观察的稳定范围**：通过阈值后，审计中的 `stable_scopes` 给出精确 `region/client_cap/route_seq/actual_percent/effective interval/measurement ids`，不会外推到其他地区、能力或更高流量。
- **路由能力与队列能力分开**：例如 coast/v2 下 r12 写 `compact`，清单中只有 r12 的 `input_formats` 含 `compact`；coast/v1 下 r12 写 `plain`，r11/r12 都可接手。
- **交接安全**：`claim --format compact --by-build r11` 会被拒绝；同一作业代次仍活跃时，另一个版本再次领取也会被拒绝。来源资料中若已有冲突，作业面板会显示 `double_delivery`。
- **容量丢失即冻结**：最后一个兼容处理实例离线、或作业等待但无可靠归宿时，控制台派生 `region/client_cap/build/format` 冻结范围，并列出等待作业。容量恢复后派生冻结自动消失。
- **回撤完成条件**：对应回撤 seq 已有 0% 生效回执、回撤后观察到旧版本成功通行、所有区域内在途/等待任务仍有可靠兼容归宿。rel-1 在演练时间线中首次满足于 53ms；之后 rel-4 造成的新 v2 流量会单独标记，不抹掉 rel-1 已闭环的审计事实。
- **重复点击与晚到确认**：所有不带幂等键的点击都会作为不同请求保存；相同 `--idem-key` 重放只返回首次事件。更新的 pause/rollback 围栏会阻止把旧 seq 的晚到回执当成继续放量许可；回执仍会保存并用于历史归因。

### 当前演练资料应得到的结论

- `rel-1 / coast-v2`：seq=1 的健康候选样本只有 24/40，尚缺 16；到达 28/31ms 的故障区间显示候选 r12 与对照 r11 在同一时间窗受共享依赖 `lex` 影响，判定为 `shared_dependency_incident`，只能暂停。c-can 在 43ms 离线后，compact 失去可靠处理者，控制台冻结 `coast/v2/r12/compact`；49ms 后 job-8 为等待状态并出现在值班面板。c-new 在 53ms 上线后归宿恢复，rel-1 回撤首次闭环于 53ms。
- `rel-2 / inland-v2`：申请目标为 r12，但 i-can 实际加载 r11，状态为 `TARGET_BLOCKED`，没有 route_applied 时不得放量。
- `rel-3 / coast-v1`：seq=4 区间 `[62,80)` 的 r12/r11 样本分别为 36/59，均达到 min_each=30，错误率差约 -1.70pp，支持且仅支持 `coast/v1/seq=4/10%` 这一稳定范围。
- `rel-4 / coast-v2`：seq=5 健康区间错误率差约 13.64pp，超过 2pp，应发出回撤。
- seq=2 的 9 个 r12 样本位于 `dep=unknown` 区间，不能计入健康证据。

### 本地操作示例

```sh
STATE=/tmp/release-console-demo.json

python release_console.py --state "$STATE" propose rel-5 \
  --region coast --cap v2 --build r12 --percent 10 --at 100 \
  --idem-key click-1

# 超时后可用同一幂等键安全重试；不带键则会记录为另一次请求
python release_console.py --state "$STATE" propose rel-5 \
  --region coast --cap v2 --build r12 --percent 10 --at 101 \
  --idem-key click-1

# 暂停/回撤可在 route_applied 未到时先记录；重复发布不会越过未闭环围栏
python release_console.py --state "$STATE" rollback rel-5 --at 101 --seq 7
python release_console.py --state "$STATE" applied \
  --seq 7 --region coast --cap v2 --percent 0 --at 108

# 如果使用的是 pause，确认后必须显式 resume；pause 后的重复 propose 不会自动恢复
python release_console.py --state "$STATE" pause rel-6 --at 101 --seq 7
python release_console.py --state "$STATE" resume rel-6 --at 110 --seq 8
```

测试：

```sh
python -m unittest discover -s tests -v
```

