# 演练资料格式

解析服务发布演练，流量范围由 region 和 client_cap 共同定义。观察是来源系统已聚合的计数，不是发布结论。构建编号、队列格式编号和路由代次是不同的标识。

## `builds` 版本与消息格式能力

region 为已登记的发布地区。input_formats 是可领取的工作格式，emit_by_cap 给出不同客户端能力的写入格式。ready_slots 是实例注册声明，实际可接纳还要看实例回执。

字段为 `region`、`build`、`input_formats`、`emit_by_cap`、`ready_slots`。

## `policy` 发布组的观察下限和归属

window_ms 是每次路由变化后观察区间长度，min_each 是两组各自最小样本量，max_excess_pp 为候选较对照可接受的错误率百分点差，dependency 是两组共享依赖。

字段为 `region`、`client_cap`、`window_ms`、`min_each`、`max_excess_pp`、`dependency`。

## `observations` 控制请求、实例和路由回执

at_ms 表示到达，item 是请求号、实例、路由或作业标识。seq 为路由或领取代次。value 依 kind 表示流量百分数、已加载构建、任务格式或状态。cap 为空时作用于整区。 请求表示操作员意图，只有实际路由回执表示生效。claim_ended 不含完成。claim 的 value 为格式和领取者构建号。没有申请对应的路由统计只能用于判断现状。

字段为 `at_ms`、`kind`、`item`、`region`、`cap`、`seq`、`value`。

## `measurements` 版本分组的计数和依赖观测

from_ms 含端点，to_ms 不含端点。route_seq 为入口记录代次。served_build 为实际运行版本。dep 状态由独立探针记录，unknown 与 healthy 含义不同。记录可晚到。 各来源计数子区间不重叠，来自相同地域、客户端类型、路由代次和版本的相邻子区间可以合计。健康未知或故障区间不提供独立候选回归证据。

字段为 `arrived_ms`、`region`、`cap`、`route_seq`、`from_ms`、`to_ms`、`served_build`、`total`、`errors`、`dep`。
