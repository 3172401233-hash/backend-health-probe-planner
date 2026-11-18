# backend-health-probe-planner

Python 后端健康探测规划库：同时消费**主动探测结果**（HTTP / TCP / gRPC）和
**真实请求的被动统计**，输出每个 endpoint 的 `healthy` / `degraded` /
`ejected` 状态以及下一次探测计划。能识别同一机架（rack）或同一依赖上的
相关性故障，避免全局网络抖动时把整片后端一起摘除。

## 快速开始

```python
from health_probe_planner import (
    EndpointSpec, EndpointState, HealthProbePlanner,
    PassiveStats, ProbeResult, ProbeType,
)

planner = HealthProbePlanner()
planner.add_endpoint(EndpointSpec(
    endpoint_id="api-1",
    probe_type=ProbeType.HTTP,
    rack="rack-1",
    dependencies=("user-db",),
    failure_status_codes=frozenset({500, 503}),
))

planner.record_probe(ProbeResult(endpoint_id="api-1", success=False, timed_out=True), now=0)
planner.record_passive(PassiveStats(endpoint_id="api-1", total=100, errors=30, timestamp=0), now=0)

states = planner.evaluate(now=1)          # {"api-1": EndpointState.DEGRADED, ...}
plans = planner.plan_probes(now=1)        # 受探测预算约束的下一次探测计划
```

时间一律由调用方注入（`now`），规划器本身不读系统时钟，便于测试与重放。

## 状态机

```
                 连续失败 ≥ degraded_failure_threshold
                 或被动错误率 ≥ passive_error_rate_degraded
                 或任一依赖不健康
        ┌────────┬─────────────────────────────────────┐
        ▼        │                                     │
   ┌─────────┐   │   连续失败 ≥ eject_failure_threshold     ┌──────────┐
   │ HEALTHY │───┼── 或被动错误率 ≥ passive_error_rate_eject ─▶│ EJECTED  │
   └─────────┘   │                                     └──────────┘
        ▲    └────────────────────▶┌──────────┐     │
        │                            │ DEGRADED  │◀────┘
        │                            └──────────┘
        └──────────────────────────────────────────────────┘
              连续成功 ≥ recovery_success_threshold
              且距上次状态变更 ≥ min_dwell_seconds
```

规则要点：

- **恶化立即生效**；**恢复必须满足连续成功窗口 + 最小驻留时间**（min dwell），
  两者共同保证状态不会来回抖动。
- 依赖不健康只把 endpoint 压到 `degraded`，**永远不会**仅因依赖而 `ejected`
  —— 主接口仍能降级返回。
- 探测结果中 `dependency_failures` 非空时，失败会沿依赖关系传播到所有共享
  该依赖的 endpoint（同样只降级）。

## 证据与阈值

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `degraded_failure_threshold` | 2 | 连续主动探测失败进入 `degraded` |
| `eject_failure_threshold` | 3 | 连续主动探测失败进入 `ejected` |
| `recovery_success_threshold` | 2 | 恢复所需连续成功次数 |
| `passive_error_rate_degraded` | 0.2 | 被动错误率降级阈值 |
| `passive_error_rate_eject` | 0.5 | 被动错误率摘除阈值 |
| `passive_min_samples` | 20 | 被动数据最少样本数，不足不采信 |
| `max_passive_age` | 30s | 超过该年龄的被动数据不再是新鲜证据 |
| `correlation_threshold` | 0.5 | 相关组内失败占比达到该值即判定相关性故障 |
| `min_correlation_group_size` | 2 | 小于该规模的组不触发相关性抑制 |
| `min_dwell_seconds` | 5s | 状态好转前最小驻留时间（防抖动） |

判定为失败的条件：`success=False`、超时（`timed_out`）、或状态码命中
endpoint 的 `failure_status_codes`（HTTP 5xx、gRPC status 等均适用）。

## 相关性故障（fail-open）

相关性组 = 同一 rack 的所有 endpoint ∪ 共享同一依赖的所有 endpoint。
`evaluate()` 先独立计算每个 endpoint 的候选状态，再检查组内候选 `ejected`
的占比：达到 `correlation_threshold` 时判定为基础设施/网络层故障，
**组内所有待摘除的 endpoint 被压制为 `degraded` 而不是 `ejected`**——
即 fail-open：宁可放行可能异常的流量，也不在全局故障时把容量清零。

- 组规模小于 `min_correlation_group_size` 时不抑制，单实例故障照常摘除。
- 组内只有部分实例失败（占比低于阈值）时，失败实例照常摘除。

## 探测计划与预算

`plan_probes(now, budget)` 返回到期探测，数量不超过预算（默认
`probe_budget`，`budget=0` 时不安排任何探测，状态仍可由被动数据驱动）：

- **退避**：间隔 = `base_probe_interval * 2^连续失败次数`，上限
  `max_probe_interval`；`ejected` endpoint 使用更长的
  `ejected_probe_interval` 做低频恢复检查。
- **抖动**：间隔乘以 `[1-jitter, 1+jitter]` 的随机因子，避免成群同时探测。
- **优先级**：预算紧张时 `ejected` endpoint 优先（尽快发现恢复），
  其次按过期程度排序。被规划的探测会推进各自的 `next_probe_at`，
  不会重复安排。

## 时钟回拨

规划器内部时间单调不回退：传入的 `now` 若小于已见时间，会被钳制并计入
`clock_regressions`。被动数据年龄按 `max(0, now - timestamp)` 计算，回拨
不会让新鲜证据突然过期；`next_probe_at` 也永不向过去移动。

## fail-open / fail-closed 边界

| 场景 | 行为 | 模式 |
| --- | --- | --- |
| 相关性故障（整 rack / 共享依赖大面积失败） | 压制为 `degraded`，保留流量 | **fail-open**（默认，`fail_open_on_correlation=True`） |
| 同上，但配置 `fail_open_on_correlation=False` | 照常全部 `ejected` | **fail-closed** |
| 单实例达到摘除阈值 | `ejected` | fail-closed |
| 依赖不健康、主接口正常 | `degraded`，不摘除 | fail-open |
| 被动数据过旧 / 样本不足 | 不作为证据，维持现状 | fail-open |
| 探测预算耗尽 | 不安排新探测，状态仅靠已有证据 | fail-open |
| 时钟回拨 | 时间钳制、证据不清空 | fail-open |

## 测试

```bash
python -m unittest
```

覆盖：偶发超时、连续失败摘除、错误码判定、依赖单独降级与传播、相关性
故障 fail-open/fail-closed、探测预算耗尽、退避与抖动边界、被动数据过期、
时钟回拨、恢复后再次失败、交替结果下状态不抖动。
