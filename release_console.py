#!/usr/bin/env python3
"""本地发布控制台：版本核验、证据分级、任务兼容性与回撤闭环。

仅使用 Python 标准库。设计要点：

* 资料文件是只读事实源；操作员请求、回执和冻结记录写入本地 JSON 状态。
* 路由是否生效只由 route_applied 回执证明，晚到回执只用于归因，不会自动放行。
* 样本按 region + client_cap + route_seq + 实际生效区间归属。
* shared dependency 故障区间单独列示；候选/对照同时受损时只触发依赖暂停。
* 队列格式能力与路由 client_cap 分开判断，并跟踪作业代次、等待和互斥领取。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable

from materials import list_feeds, read_feed

ROOT = Path(__file__).resolve().parent
DEFAULT_MATERIALS = ROOT / "materials"
DEFAULT_STATE = Path("release-console-state.json")
STATE_FORMAT = "release-console-state/1"

REQUEST_KINDS = {"propose", "pause", "rollback"}
TERMINAL_JOB_KINDS = {"finished", "claim_ended"}


class ConsoleError(Exception):
    """输入或状态不合法。"""


class SafetyError(ConsoleError):
    """该动作违反发布/领取安全约束。"""


def version_key(value: str) -> tuple[Any, ...]:
    """让 r9 排在 r10 前面，同时保留非数字标识的稳定排序。"""
    parts = re.split(r"(\d+)", str(value))
    return tuple(int(p) if p.isdigit() else p for p in parts)


def error_rate(row: dict[str, Any]) -> float | None:
    total = int(row.get("total", 0))
    if total <= 0:
        return None
    return int(row["errors"]) / total


def excess_pp(candidate: dict[str, Any], control: dict[str, Any]) -> float | None:
    cr = error_rate(candidate)
    xr = error_rate(control)
    if cr is None or xr is None:
        return None
    return round((cr - xr) * 100.0, 3)


def overlap(a_from: float, a_to: float, b_from: float, b_to: float) -> bool:
    return max(a_from, b_from) < min(a_to, b_to)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


class ReleaseConsole:
    """发布控制台的核心状态与判定。

    ``state_path`` 为 None 时只在内存中保存操作员事件。测试可直接传入
    materials_dir 指向临时资料目录。
    """

    def __init__(
        self,
        materials_dir: str | Path = DEFAULT_MATERIALS,
        state_path: str | Path | None = DEFAULT_STATE,
    ) -> None:
        self.materials_dir = Path(materials_dir)
        self.state_path = Path(state_path) if state_path else None
        self.builds: list[dict[str, Any]] = []
        self.policies: list[dict[str, Any]] = []
        self.material_observations: list[dict[str, Any]] = []
        self.material_measurements: list[dict[str, Any]] = []
        self.reload_materials()
        self.state = self._default_state()
        if self.state_path and self.state_path.exists():
            self.load_state()

    # ------------------------------------------------------------------ setup

    def _default_state(self) -> dict[str, Any]:
        return {
            "format": STATE_FORMAT,
            "specs": {},
            "events": [],
            "manual_freezes": [],
            "view_at": None,
            "idempotency": {},
        }

    def reload_materials(self) -> None:
        """重新读取下一版清单和最新资料；不覆盖本地操作记录。"""
        available = set(list_feeds(self.materials_dir))
        required = {"builds", "policy", "observations", "measurements"}
        missing = required - available
        if missing:
            raise ConsoleError(f"资料目录缺少：{', '.join(sorted(missing))}")
        self.builds = list(read_feed("builds", root=self.materials_dir))
        self.policies = list(read_feed("policy", root=self.materials_dir))
        self.material_observations = list(read_feed("observations", root=self.materials_dir))
        self.material_measurements = list(read_feed("measurements", root=self.materials_dir))
        for idx, row in enumerate(self.material_measurements):
            row.setdefault("_id", f"m{idx + 1}")

    def save_state(self) -> None:
        if not self.state_path:
            return
        self.state["format"] = STATE_FORMAT
        atomic_write_json(self.state_path, self.state)

    def load_state(self) -> None:
        assert self.state_path
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConsoleError(f"状态文件不是有效 JSON：{self.state_path}") from exc
        if not isinstance(loaded, dict) or loaded.get("format") != STATE_FORMAT:
            raise ConsoleError("状态文件格式不受支持")
        base = self._default_state()
        base.update(loaded)
        self.state = base

    # ------------------------------------------------------------- time/events

    def latest_time(self) -> int:
        times = [int(e["at_ms"]) for e in self.all_observations()]
        times += [int(m["arrived_ms"]) for m in self.all_measurements()]
        if self.state.get("view_at") is not None:
            times.append(int(self.state["view_at"]))
        return max(times, default=0)

    def default_at(self, at: int | None = None) -> int:
        if at is not None:
            return int(at)
        return self.latest_time() + 1

    def view_at(self, at: int | None = None) -> int:
        if at is not None:
            return int(at)
        if self.state.get("view_at") is not None:
            return int(self.state["view_at"])
        return self.latest_time()

    def wait_to(self, at: int) -> dict[str, Any]:
        self.state["view_at"] = int(at)
        self.save_state()
        return self.dashboard(at=int(at))

    def advance_wait(self, milliseconds: int) -> dict[str, Any]:
        current = self.view_at()
        target = max(current + int(milliseconds), self.latest_time())
        return self.wait_to(target)

    def all_observations(self) -> list[dict[str, Any]]:
        return [*self.material_observations, *self.state["events"]]

    def all_measurements(self) -> list[dict[str, Any]]:
        return [*self.material_measurements, *self.state.get("measurements", [])]

    def observations_until(self, at: int) -> list[dict[str, Any]]:
        return [e for e in self.all_observations() if int(e["at_ms"]) <= at]

    def measurements_until(self, at: int) -> list[dict[str, Any]]:
        return [m for m in self.all_measurements() if int(m["arrived_ms"]) <= at]

    def append_event(
        self,
        event: dict[str, Any],
        *,
        idem_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """追加操作员事件。

        相同 idempotency_key 重放时返回首次事件和 duplicate=True。没有键的
        重复点击仍会作为不同请求保存，由围栏/状态归并，而不是被静默丢弃。
        """
        if idem_key:
            key = str(idem_key)
            if key in self.state["idempotency"]:
                event_id = self.state["idempotency"][key]
                old = next((e for e in self.state["events"] if e.get("event_id") == event_id), None)
                if old is None:
                    raise ConsoleError("幂等键指向的事件不存在")
                return old, True
            event["idem_key"] = key
        event.setdefault("event_id", uuid.uuid4().hex)
        event.setdefault("recorded_at_ms", self.latest_time() + 1)
        self.state["events"].append(event)
        if idem_key:
            self.state["idempotency"][str(idem_key)] = event["event_id"]
        cursor = self.state.get("view_at")
        if cursor is not None and int(event["at_ms"]) > int(cursor):
            self.state["view_at"] = int(event["at_ms"])
        self.save_state()
        return event, False

    # ------------------------------------------------------------------ specs

    def builds_in_region(self, region: str) -> list[dict[str, Any]]:
        return [b for b in self.builds if b["region"] == region]

    def build_info(self, region: str, build: str) -> dict[str, Any] | None:
        return next((b for b in self.builds if b["region"] == region and b["build"] == build), None)

    def policy_for(self, region: str, cap: str) -> dict[str, Any] | None:
        return next((p for p in self.policies if p["region"] == region and p["client_cap"] == cap), None)

    def source_proposals(self, release: str) -> list[dict[str, Any]]:
        return [
            e
            for e in self.material_observations
            if e.get("item") == release and e.get("kind") == "propose"
        ]

    def set_spec(
        self,
        release: str,
        *,
        region: str | None = None,
        cap: str | None = None,
        build: str | None = None,
        baseline: str | None = None,
        desired_percent: float | None = None,
    ) -> dict[str, Any]:
        spec = self.state["specs"].setdefault(release, {"release": release})
        source = self.source_proposals(release)[0] if self.source_proposals(release) else None
        if region is None and source:
            region = source["region"]
        if cap is None and source:
            cap = source["cap"]
        if desired_percent is None and source:
            desired_percent = source["value"]
        for key, value in (
            ("region", region),
            ("cap", cap),
            ("build", build),
            ("baseline", baseline),
            ("desired_percent", desired_percent),
        ):
            if value is not None:
                spec[key] = value
        self.save_state()
        return self.resolve_spec(release)

    def resolve_spec(self, release: str, at: int | None = None) -> dict[str, Any]:
        at = self.view_at(at)
        explicit = dict(self.state["specs"].get(release, {}))
        requests = [
            e
            for e in self.observations_until(at)
            if e.get("item") == release and e.get("kind") in REQUEST_KINDS
        ]
        proposals = [e for e in requests if e["kind"] == "propose"]
        first = proposals[0] if proposals else (requests[0] if requests else None)
        if first:
            explicit.setdefault("region", first["region"])
            explicit.setdefault("cap", first["cap"])
        if proposals:
            explicit.setdefault("desired_percent", proposals[-1].get("value"))
        region = explicit.get("region")
        if region and not explicit.get("build"):
            rows = self.builds_in_region(region)
            if rows:
                explicit["build"] = max(rows, key=lambda b: version_key(b["build"]))["build"]
                explicit["build_inferred"] = True
        if region and explicit.get("build") and not explicit.get("baseline"):
            target = explicit["build"]
            target_key = version_key(target)
            others = [b["build"] for b in self.builds_in_region(region) if b["build"] != target]
            lower = [b for b in others if version_key(b) < target_key]
            if others:
                # Prefer the nearest predecessor; never silently select the
                # oldest build when a newer previous stable exists.
                explicit["baseline"] = max(lower or others, key=version_key)
                explicit["baseline_inferred"] = True
        explicit["release"] = release
        return explicit

    # ------------------------------------------------------------- operator API

    def next_seq(self, at: int | None = None) -> int:
        at = self.view_at(at)
        seqs = [int(e.get("seq", 0)) for e in self.observations_until(at) if e.get("seq") is not None]
        return max(seqs, default=-1) + 1

    def propose(
        self,
        release: str,
        *,
        region: str,
        cap: str,
        percent: float,
        build: str | None = None,
        baseline: str | None = None,
        at: int | None = None,
        seq: int | None = None,
        request_id: str | None = None,
        idem_key: str | None = None,
    ) -> dict[str, Any]:
        at = self.default_at(at)
        if idem_key and idem_key in self.state["idempotency"]:
            event_id = self.state["idempotency"][idem_key]
            old = next((e for e in self.state["events"] if e.get("event_id") == event_id), None)
            if old is not None:
                return {
                    "event": old,
                    "duplicate": True,
                    "preflight": self.proposal_preflight(region, cap, build, at, release=release),
                }
        if not 0 <= float(percent) <= 100:
            raise ConsoleError("流量百分数必须在 0 到 100 之间")
        if build:
            self.set_spec(release, region=region, cap=cap, build=build, baseline=baseline, desired_percent=percent)
        else:
            self.set_spec(release, region=region, cap=cap, baseline=baseline, desired_percent=percent)
        event = {
            "at_ms": at,
            "kind": "propose",
            "item": release,
            "region": region,
            "cap": cap,
            "seq": int(seq if seq is not None else self.next_seq(at)),
            "value": percent,
            "request_id": request_id or uuid.uuid4().hex,
        }
        saved, duplicate = self.append_event(event, idem_key=idem_key)
        preflight = self.proposal_preflight(region, cap, self.resolve_spec(release, at).get("build"), at, release=release)
        return {"event": saved, "duplicate": duplicate, "preflight": preflight}

    def control_request(
        self,
        kind: str,
        release: str,
        *,
        at: int | None = None,
        seq: int | None = None,
        value: float | None = None,
        request_id: str | None = None,
        idem_key: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"pause", "rollback", "resume"}:
            raise ConsoleError("控制请求类型只能是 pause、resume 或 rollback")
        spec = self.resolve_spec(release)
        if not spec.get("region") or not spec.get("cap"):
            raise ConsoleError(f"发布 {release} 尚没有 region/cap 上下文")
        at = self.default_at(at)
        if kind == "rollback" and value is None:
            value = 0
        event = {
            "at_ms": at,
            "kind": kind,
            "item": release,
            "region": spec["region"],
            "cap": spec["cap"],
            "seq": int(seq if seq is not None else self.next_seq(at)),
            "value": value,
            "request_id": request_id or uuid.uuid4().hex,
        }
        saved, duplicate = self.append_event(event, idem_key=idem_key)
        return {"event": saved, "duplicate": duplicate}

    def record_route_applied(
        self,
        *,
        seq: int,
        region: str,
        cap: str,
        percent: float,
        at: int | None = None,
        item: str = "local-route",
        idem_key: str | None = None,
    ) -> dict[str, Any]:
        at = self.default_at(at)
        event = {
            "at_ms": at,
            "kind": "route_applied",
            "item": item,
            "region": region,
            "cap": cap,
            "seq": int(seq),
            "value": percent,
        }
        saved, duplicate = self.append_event(event, idem_key=idem_key)
        return {"event": saved, "duplicate": duplicate}

    def record_instance(
        self,
        *,
        instance: str,
        region: str,
        build: str | None = None,
        offline: bool = False,
        at: int | None = None,
        seq: int = 1,
    ) -> dict[str, Any]:
        if offline == bool(build):
            raise ConsoleError("实例回执必须二选一：--build VERSION 或 --offline")
        at = self.default_at(at)
        event = {
            "at_ms": at,
            "kind": "instance",
            "item": instance,
            "region": region,
            "cap": None,
            "seq": int(seq),
            "value": "offline" if offline else build,
        }
        saved, _ = self.append_event(event)
        return saved

    def record_claim(
        self,
        *,
        job: str,
        region: str,
        cap: str,
        seq: int,
        data_format: str,
        by_build: str,
        at: int | None = None,
        enforce_safety: bool = True,
    ) -> dict[str, Any]:
        at = self.default_at(at)
        info = self.build_info(region, by_build)
        if enforce_safety and (not info or data_format not in info["input_formats"]):
            raise SafetyError(f"{by_build} 不能领取 {data_format} 任务；已拒绝写入领取记录")
        jobs = self.jobs_snapshot(at=at, region=region)
        existing = jobs.get(job)
        if enforce_safety and existing and existing.get("active_claims"):
            same_generation = [c for c in existing["active_claims"] if c["seq"] == seq]
            for claim in same_generation:
                if claim.get("by_build") != by_build or claim.get("format") != data_format:
                    raise SafetyError(f"任务 {job} seq={seq} 已由 {claim.get('by_build')}/{claim.get('format')} 持有；同一代次不能改交给另一版本")
                prior = next(
                    (
                        e
                        for e in reversed(self.all_observations())
                        if e.get("kind") == "claim"
                        and e.get("item") == job
                        and int(e.get("seq") or 0) == seq
                        and e.get("value") == f"{data_format}:{by_build}"
                    ),
                    None,
                )
                if prior is not None:
                    return prior
            other_generation = [c for c in existing["active_claims"] if c["seq"] != seq]
            if other_generation:
                raise SafetyError(f"任务 {job} 已被 {other_generation} 持有；不能同时交给两个版本")
        event = {
            "at_ms": at,
            "kind": "claim",
            "item": job,
            "region": region,
            "cap": cap,
            "seq": int(seq),
            "value": f"{data_format}:{by_build}",
        }
        saved, _ = self.append_event(event)
        return saved

    def record_job_terminal(
        self,
        kind: str,
        *,
        job: str,
        seq: int,
        reason: str | None = None,
        at: int | None = None,
    ) -> dict[str, Any]:
        if kind not in {"claim_ended", "finished"}:
            raise ConsoleError("作业终态只能是 claim_ended 或 finished")
        at = self.default_at(at)
        existing = next(
            (e for e in self.all_observations() if e.get("item") == job and e.get("seq") == seq),
            None,
        )
        event = {
            "at_ms": at,
            "kind": kind,
            "item": job,
            "region": existing["region"] if existing else None,
            "cap": existing.get("cap") if existing else None,
            "seq": int(seq),
            "value": reason,
        }
        saved, _ = self.append_event(event)
        return saved

    def record_measurement(self, row: dict[str, Any]) -> dict[str, Any]:
        required = {"arrived_ms", "region", "cap", "route_seq", "from_ms", "to_ms", "served_build", "total", "errors", "dep"}
        missing = required - row.keys()
        if missing:
            raise ConsoleError(f"measurement 缺少字段：{sorted(missing)}")
        if not self.state.get("measurements"):
            self.state["measurements"] = []
        row = dict(row)
        row["_id"] = row.get("_id") or f"local-m{uuid.uuid4().hex}"
        self.state["measurements"].append(row)
        self.save_state()
        return row

    def freeze_scope(
        self,
        *,
        region: str,
        cap: str,
        candidate_build: str | None = None,
        data_format: str | None = None,
        reason: str,
        at: int | None = None,
    ) -> dict[str, Any]:
        at = self.default_at(at)
        freeze = {
            "event_id": uuid.uuid4().hex,
            "at_ms": at,
            "region": region,
            "cap": cap,
            "build": candidate_build,
            "format": data_format,
            "reason": reason,
            "active": True,
        }
        self.state["manual_freezes"].append(freeze)
        self.save_state()
        return freeze

    def unfreeze_scope(
        self,
        *,
        region: str,
        cap: str,
        candidate_build: str | None = None,
        data_format: str | None = None,
        at: int | None = None,
    ) -> dict[str, Any]:
        at = self.default_at(at)
        row = {
            "event_id": uuid.uuid4().hex,
            "at_ms": at,
            "region": region,
            "cap": cap,
            "build": candidate_build,
            "format": data_format,
            "active": False,
        }
        self.state["manual_freezes"].append(row)
        self.save_state()
        return row

    # ------------------------------------------------------------- instances

    def instance_reports(self, at: int | None = None, region: str | None = None) -> dict[str, dict[str, Any]]:
        at = self.view_at(at)
        latest: dict[str, dict[str, Any]] = {}
        rows = [e for e in self.observations_until(at) if e.get("kind") == "instance"]
        if region:
            rows = [e for e in rows if e.get("region") == region]
        for event in sorted(rows, key=lambda e: (int(e["at_ms"]), int(e.get("seq") or 0))):
            latest[event["item"]] = event
        return latest

    def online_instances(self, region: str, at: int | None = None) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for instance, event in self.instance_reports(at=at, region=region).items():
            value = event.get("value")
            if value and value != "offline" and self.build_info(region, value):
                result.setdefault(value, []).append(instance)
        return result

    def build_explicitly_lost(self, region: str, build: str, at: int) -> bool:
        """已知该版本实例最后回执均为 offline，且没有新的在线回执。"""
        online = self.online_instances(region, at).get(build, [])
        if online:
            return False
        reports = self.instance_reports(at=at, region=region)
        saw_build = False
        for event in reports.values():
            # latest report per instance; an offline event only proves loss when
            # that instance had previously reported this build.
            if event.get("value") == "offline":
                history = [
                    e
                    for e in self.observations_until(at)
                    if e.get("kind") == "instance"
                    and e.get("item") == event["item"]
                    and e.get("value") == build
                ]
                if history:
                    saw_build = True
            elif event.get("value") == build:
                saw_build = True
        return saw_build

    def verify_target(self, region: str, target: str | None, at: int | None = None) -> dict[str, Any]:
        at = self.view_at(at)
        if not target:
            return {"verified": False, "status": "target_unspecified", "issues": ["申请未指定目标版本"]}
        info = self.build_info(region, target)
        if not info:
            return {"verified": False, "status": "build_unknown", "target": target, "issues": ["版本清单中没有该构建"]}
        reports = self.instance_reports(at=at, region=region)
        online = self.online_instances(region, at)
        target_instances = online.get(target, [])
        wrong: list[dict[str, Any]] = []
        offline: list[str] = []
        unknown: list[dict[str, Any]] = []
        for name, event in reports.items():
            value = event.get("value")
            if value == target:
                continue
            if value == "offline":
                offline.append(name)
            elif self.build_info(region, value):
                wrong.append({"instance": name, "loaded_build": value, "at_ms": event["at_ms"]})
            else:
                unknown.append({"instance": name, "value": value})
        issues: list[str] = []
        if not target_instances:
            if wrong:
                issues.append("目标实例实际加载的不是申请版本：" + ", ".join(f"{x['instance']}={x['loaded_build']}" for x in wrong))
            elif offline:
                issues.append("目标实例当前无在线加载回执：" + ", ".join(offline))
            else:
                issues.append("尚未收到目标版本的实例加载回执")
        elif wrong:
            issues.append("已有目标版本实例，但同一发布地区仍有错误加载实例：" + ", ".join(f"{x['instance']}={x['loaded_build']}" for x in wrong))
        if unknown:
            issues.append("存在无法识别的实例回执：" + ", ".join(f"{x['instance']}={x['value']}" for x in unknown))
        # One correct canary identifies the loaded build, but a mixed/unknown
        # fleet is not safe to expand.
        verified = bool(target_instances) and not wrong and not unknown
        return {
            "verified": verified,
            "status": "verified" if verified else "mismatch_or_missing",
            "target": target,
            "target_capabilities": {
                "input_formats": info["input_formats"],
                "emit_by_cap": info["emit_by_cap"],
                "declared_ready_slots": info["ready_slots"],
            },
            "target_instances": target_instances,
            "wrong_build_instances": wrong,
            "offline_instances": offline,
            "unknown_instances": unknown,
            "issues": issues,
        }

    # ------------------------------------------------------------- compatibility

    def _active_claim_builds(self, region: str, data_format: str, at: int) -> dict[str, int]:
        """直接从领取/终态事件统计仍在持有者手中的格式，避免与作业快照递归。"""
        status: dict[tuple[str, int], tuple[str, str, str]] = {}
        for event in self.observations_until(at):
            if event.get("region") != region:
                continue
            kind = event.get("kind")
            job = event.get("item")
            seq = int(event.get("seq") or 0)
            key = (job, seq)
            if kind == "claim":
                value = str(event.get("value") or "")
                fmt, build = value.rsplit(":", 1) if ":" in value else (value, "")
                status[key] = ("active", fmt, build)
            elif kind in {"claim_ended", "finished"}:
                previous = status.get(key)
                status[key] = (kind, previous[1] if previous else "", previous[2] if previous else "")
        counts: dict[str, int] = {}
        for state, fmt, build in status.values():
            if state == "active" and fmt == data_format and build:
                counts[build] = counts.get(build, 0) + 1
        return counts

    def reliable_destinations(self, region: str, data_format: str, at: int | None = None) -> list[dict[str, Any]]:
        at = self.view_at(at)
        online = self.online_instances(region, at)
        active_claims = self._active_claim_builds(region, data_format, at)
        result: list[dict[str, Any]] = []
        for info in self.builds_in_region(region):
            build = info["build"]
            if data_format not in info["input_formats"]:
                continue
            evidence: list[str] = []
            reliable = False
            confidence = "none"
            instances = online.get(build, [])
            if instances:
                reliable = True
                confidence = "instance_receipt"
                evidence.append("online=" + ",".join(instances))
            serving = [
                m
                for m in self.measurements_until(at)
                if m["region"] == region and m["served_build"] == build and int(m["total"] - m["errors"]) > 0
                and int(m["to_ms"]) <= at
            ]
            explicitly_lost = self.build_explicitly_lost(region, build, at)
            if not reliable and not explicitly_lost and info["ready_slots"] > 0 and serving:
                reliable = True
                confidence = "serving_observation"
                evidence.append(f"successful_samples={sum(int(m['total']-m['errors']) for m in serving)}")
            if not reliable and not explicitly_lost and active_claims.get(build):
                # A live claim is evidence that this handler currently owns the
                # work, even if baseline instances do not emit target receipts.
                reliable = True
                confidence = "active_claim"
                evidence.append(f"active_claims={active_claims[build]}")
            if info["ready_slots"] > 0:
                evidence.append(f"declared_ready_slots={info['ready_slots']}")
            if explicitly_lost:
                confidence = "explicitly_lost"
                evidence.append("all_known_instances_offline")
            result.append(
                {
                    "build": build,
                    "can_claim_format": True,
                    "reliable": reliable,
                    "confidence": confidence,
                    "evidence": evidence,
                    "declared_ready_slots": info["ready_slots"],
                    "online_instances": instances,
                }
            )
        return result

    def compatibility_matrix(self, region: str, cap: str, target: str | None, at: int | None = None) -> dict[str, Any]:
        at = self.view_at(at)
        rows = []
        for build in [b["build"] for b in self.builds_in_region(region)]:
            info = self.build_info(region, build)
            emitted = info["emit_by_cap"].get(cap)
            if emitted is None:
                continue
            processors = [d for d in self.reliable_destinations(region, emitted, at) if d["can_claim_format"]]
            reliable = [d for d in processors if d["reliable"]]
            rows.append(
                {
                    "build": build,
                    "route_client_cap": cap,
                    "emitted_format": emitted,
                    "manifest_claimable_builds": [
                        b["build"] for b in self.builds_in_region(region) if emitted in b["input_formats"]
                    ],
                    "reliable_destinations": reliable,
                    "all_destination_evidence": processors,
                    "old_handler_can_continue": any(
                        emitted in (self.build_info(region, b) or {}).get("input_formats", [])
                        for b in [x["build"] for x in self.builds_in_region(region) if x["build"] != build]
                    ),
                }
            )
        target_row = next((r for r in rows if r["build"] == target), None)
        return {"region": region, "cap": cap, "target": target, "rows": rows, "target_row": target_row}

    # -------------------------------------------------------------------- jobs

    def jobs_snapshot(self, at: int | None = None, region: str | None = None) -> dict[str, dict[str, Any]]:
        at = self.view_at(at)
        jobs: dict[str, dict[str, Any]] = {}
        indexed_events = list(enumerate(self.observations_until(at)))
        job_events = [
            (insertion, event)
            for insertion, event in indexed_events
            if event.get("kind") in {"claim", "claim_ended", "finished"}
        ]
        # Equal timestamps are ambiguous; source/append order is the stable
        # tie-break and prevents a same-ms terminal from being reordered after
        # its claim solely by event kind.
        job_events.sort(key=lambda pair: (int(pair[1]["at_ms"]), pair[0]))
        for _insertion, event in job_events:
            kind = event.get("kind")
            job_id = event["item"]
            job = jobs.setdefault(
                job_id,
                {"id": job_id, "region": event.get("region"), "cap": event.get("cap"), "generations": {}},
            )
            if region and job.get("region") != region:
                continue
            seq = int(event.get("seq") or 0)
            gen = job["generations"].setdefault(seq, {"seq": seq, "events": [], "claims": []})
            gen["events"].append({"kind": kind, "at_ms": event["at_ms"], "value": event.get("value")})
            if kind == "claim":
                value = str(event.get("value") or "")
                if ":" in value:
                    data_format, by_build = value.rsplit(":", 1)
                else:
                    data_format, by_build = value, ""
                claim = {"format": data_format, "by_build": by_build, "at_ms": event["at_ms"]}
                gen["claims"].append(claim)
                gen["status"] = "active"
                gen.setdefault("format", data_format)
                gen.setdefault("by_build", by_build)
                gen.setdefault("claimed_at", event["at_ms"])
                gen["region"] = event.get("region")
                gen["cap"] = event.get("cap")
                holders = {(c["format"], c["by_build"]) for c in gen["claims"]}
                gen["conflicting_claims"] = len(holders) > 1
                info = self.build_info(event["region"], by_build) if event.get("region") else None
                # A generation is supportable only if every observed claimant
                # supports its claimed format.
                prior_ok = gen.get("handler_supports_format", True)
                this_ok = bool(info and data_format in info["input_formats"])
                gen["handler_supports_format"] = prior_ok and this_ok
            elif kind == "claim_ended":
                gen["status"] = "waiting"
                gen["end_reason"] = event.get("value")
                gen["ended_at"] = event["at_ms"]
            elif kind == "finished":
                gen["status"] = "finished"
                gen["finished_at"] = event["at_ms"]
        result: dict[str, dict[str, Any]] = {}
        for job_id, job in jobs.items():
            if region and job.get("region") != region:
                continue
            gens = [job["generations"][k] for k in sorted(job["generations"])]
            active = [g for g in gens if g.get("status") == "active"]
            waiting = [g for g in gens if g.get("status") == "waiting"]
            latest = gens[-1] if gens else {}
            double_delivery = len(active) > 1 or any(g.get("conflicting_claims") for g in active)
            destination_checks = []
            unresolved = False
            for gen in active + ([latest] if latest.get("status") == "waiting" else []):
                fmt = gen.get("format")
                if not fmt:
                    continue
                dests = self.reliable_destinations(job["region"], fmt, at)
                reliable = [d for d in dests if d["reliable"]]
                check = {
                    "seq": gen["seq"],
                    "status": gen.get("status"),
                    "format": fmt,
                    "by_build": gen.get("by_build"),
                    "reliable_destinations": reliable,
                }
                if gen.get("status") == "active":
                    # The active holder itself is accepted as destination evidence.
                    holder_ok = gen.get("handler_supports_format", False)
                    holder_lost = self.build_explicitly_lost(job["region"], gen.get("by_build"), at)
                    check["holder_supports_format"] = holder_ok
                    check["holder_explicitly_lost"] = holder_lost
                    if not holder_ok or holder_lost or double_delivery:
                        unresolved = True
                    elif not reliable:
                        # active_claim evidence should normally make this reliable
                        unresolved = True
                elif gen.get("status") == "waiting" and not reliable:
                    unresolved = True
                destination_checks.append(check)
            status = latest.get("status", "unknown")
            result[job_id] = {
                **job,
                "latest_seq": latest.get("seq"),
                "status": "double_delivery" if double_delivery else status,
                "active_claims": active,
                "waiting_generations": waiting,
                "unfinished": status in {"active", "waiting"} or bool(active),
                "waiting": status == "waiting",
                "double_delivery": double_delivery,
                "destination_unresolved": unresolved,
                "destination_checks": destination_checks,
            }
        return result

    def waiting_jobs(self, at: int | None = None, region: str | None = None) -> list[dict[str, Any]]:
        return [j for j in self.jobs_snapshot(at=at, region=region).values() if j.get("waiting") or j.get("destination_unresolved")]

    # ---------------------------------------------------------------- freezes

    def manual_freeze_rows(self, at: int) -> list[dict[str, Any]]:
        # Manual freezes are append-only; an event with active=False closes key.
        active: dict[tuple, dict[str, Any]] = {}
        for row in self.state.get("manual_freezes", []):
            if int(row["at_ms"]) > at:
                continue
            key = (row["region"], row["cap"], row.get("build"), row.get("format"))
            if row.get("active", True):
                active[key] = row
            else:
                active.pop(key, None)
        return list(active.values())

    def derived_freezes(self, at: int | None = None) -> list[dict[str, Any]]:
        at = self.view_at(at)
        freezes: list[dict[str, Any]] = []
        # A waiting job without a destination freezes every client/build scope
        # that can emit its format.
        jobs = self.jobs_snapshot(at=at)
        blocked_formats: dict[tuple[str, str], list[str]] = {}
        for job in jobs.values():
            if not job.get("destination_unresolved"):
                continue
            for check in job.get("destination_checks", []):
                if check.get("status") in {"waiting", "active"} and not check.get("reliable_destinations"):
                    key = (job["region"], check["format"])
                    blocked_formats.setdefault(key, []).append(job["id"])
                elif check.get("holder_explicitly_lost"):
                    key = (job["region"], check["format"])
                    blocked_formats.setdefault(key, []).append(job["id"])
        for (region, fmt), job_ids in blocked_formats.items():
            for build in self.builds_in_region(region):
                for cap, emitted in build["emit_by_cap"].items():
                    if emitted != fmt:
                        continue
                    freezes.append(
                        {
                            "region": region,
                            "cap": cap,
                            "build": build["build"],
                            "format": fmt,
                            "kind": "lost_processing_capacity",
                            "waiting_jobs": sorted(set(job_ids)),
                            "reason": f"{fmt} 已无可靠处理版本；冻结该新版本接纳范围",
                        }
                    )
        # Explicit capacity loss for an emitted format even while the current
        # job's lease has not yet ended.
        for build in self.builds:
            region, b = build["region"], build["build"]
            if not self.build_explicitly_lost(region, b, at):
                continue
            for cap, fmt in build["emit_by_cap"].items():
                reliable = [d for d in self.reliable_destinations(region, fmt, at) if d["reliable"]]
                if reliable:
                    continue
                if any(f["region"] == region and f["cap"] == cap and f.get("build") == b for f in freezes):
                    continue
                related_jobs = [
                    j["id"]
                    for j in jobs.values()
                    if j.get("region") == region
                    and any(g.get("format") == fmt for g in j.get("active_claims", []))
                ]
                freezes.append(
                    {
                        "region": region,
                        "cap": cap,
                        "build": b,
                        "format": fmt,
                        "kind": "processor_offline",
                        "waiting_jobs": related_jobs,
                        "reason": f"{b} 已失去 {fmt} 处理回执；在途/等待作业不得交给不兼容旧版本",
                    }
                )
        # Deduplicate.
        dedup = {}
        for f in freezes:
            key = (f["region"], f["cap"], f.get("build"), f.get("format"), f["kind"])
            dedup[key] = f
        return list(dedup.values())

    def active_freezes(self, at: int | None = None) -> list[dict[str, Any]]:
        at = self.view_at(at)
        rows = [*self.derived_freezes(at), *self.manual_freeze_rows(at)]
        dedup = {}
        for row in rows:
            key = (row["region"], row["cap"], row.get("build"), row.get("format"))
            dedup[key] = row
        return list(dedup.values())

    def control_fence(self, release: str, at: int | None = None) -> dict[str, Any]:
        """Return pause/rollback fencing state for one release.

        A new propose click is recorded but never implicitly resumes a pause.
        Resume must be explicit; a rollback supersedes a pause.
        """
        at = self.view_at(at)
        controls = sorted(
            [
                e
                for e in self.observations_until(at)
                if e.get("item") == release and e.get("kind") in {"pause", "rollback", "resume"}
            ],
            key=lambda e: (int(e.get("seq") or 0), int(e["at_ms"])),
        )
        last_pause = next((e for e in reversed(controls) if e["kind"] == "pause"), None)
        last_clearing = next((e for e in reversed(controls) if e["kind"] in {"resume", "rollback"}), None)
        pause_active = bool(
            last_pause and (not last_clearing or int(last_clearing.get("seq") or 0) < int(last_pause.get("seq") or 0))
        )
        last_rollback = next((e for e in reversed(controls) if e["kind"] == "rollback"), None)
        rollback = self.rollback_readiness(release, at, _compute_completed=False) if last_rollback else None
        rollback_outstanding = bool(last_rollback and rollback and not rollback["complete"])
        blockers: list[str] = []
        if pause_active:
            blockers.append("暂停围栏仍有效；重复发布点击只记录为请求，不能自动恢复放量")
        if rollback_outstanding:
            blockers.append("回撤尚未闭环；只能继续校验回撤，不能推进新流量")
        return {
            "pause_active": pause_active,
            "last_pause": last_pause,
            "last_rollback": last_rollback,
            "rollback_outstanding": rollback_outstanding,
            "rollback_readiness": rollback,
            "blockers": blockers,
        }

    def proposal_preflight(
        self,
        region: str,
        cap: str,
        target: str | None,
        at: int | None = None,
        *,
        release: str | None = None,
    ) -> dict[str, Any]:
        at = self.view_at(at)
        blockers: list[str] = []
        warnings: list[str] = []
        fence = self.control_fence(release, at) if release else None
        if fence:
            blockers.extend(fence["blockers"])
        verification = self.verify_target(region, target, at)
        if not verification["verified"]:
            blockers.extend(verification["issues"])
        policy = self.policy_for(region, cap)
        if not policy:
            blockers.append(f"缺少 {region}/{cap} 观察策略")
        compat = self.compatibility_matrix(region, cap, target, at)
        target_row = compat.get("target_row")
        if target_row and not target_row["reliable_destinations"]:
            blockers.append(f"目标版本写出的 {target_row['emitted_format']} 格式当前没有可靠处理者")
        freezes = self.active_freezes(at)
        matching = [
            f
            for f in freezes
            if f["region"] == region and f["cap"] == cap and (not target or not f.get("build") or f.get("build") == target)
        ]
        if matching:
            blockers.append("适用接纳范围已冻结：" + "；".join(f["reason"] for f in matching))
        job_snapshot = self.jobs_snapshot(at=at, region=region)
        unresolved_jobs = []
        in_flight_compatibility = []
        for job in job_snapshot.values():
            if not job.get("unfinished"):
                continue
            in_flight_compatibility.append(
                {
                    "job": job["id"],
                    "status": job["status"],
                    "double_delivery": job["double_delivery"],
                    "destination_unresolved": job["destination_unresolved"],
                    "checks": job["destination_checks"],
                }
            )
            if job.get("destination_unresolved"):
                unresolved_jobs.append(job["id"])
        if unresolved_jobs:
            blockers.append("在途/等待任务缺少可靠接手者：" + ", ".join(sorted(unresolved_jobs)))
        return {
            "safe_to_issue_route": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "target_verification": verification,
            "compatibility": compat,
            "in_flight_compatibility": in_flight_compatibility,
            "matching_freezes": matching,
            "control_fence": fence,
        }

    # ---------------------------------------------------------------- routing

    def route_receipts(self, region: str, cap: str, at: int) -> list[dict[str, Any]]:
        rows = [
            e
            for e in self.observations_until(at)
            if e.get("kind") == "route_applied" and e.get("region") == region and e.get("cap") == cap
        ]
        return sorted(rows, key=lambda e: (int(e["seq"]), int(e["at_ms"])))

    def current_route(self, region: str, cap: str, at: int | None = None) -> dict[str, Any] | None:
        at = self.view_at(at)
        receipts = self.route_receipts(region, cap, at)
        return receipts[-1] if receipts else None

    def release_requests(self, release: str, at: int) -> list[dict[str, Any]]:
        return sorted(
            [e for e in self.observations_until(at) if e.get("item") == release and e.get("kind") in REQUEST_KINDS],
            key=lambda e: (int(e.get("seq") or 0), int(e["at_ms"])),
        )

    def round_evidence(self, request: dict[str, Any], spec: dict[str, Any], at: int) -> dict[str, Any]:
        region, cap, seq = request["region"], request["cap"], int(request["seq"])
        receipts = [r for r in self.route_receipts(region, cap, at) if int(r["seq"]) == seq]
        # The earliest receipt is the first proof that the generation took
        # effect. The latest receipt carries the newest reported actual value.
        first_receipt = receipts[0] if receipts else None
        receipt = receipts[-1] if receipts else None
        receipt_values = {r.get("value") for r in receipts}
        all_receipts = self.route_receipts(region, cap, at)
        next_receipt = next((r for r in all_receipts if int(r["seq"]) > seq), None)
        raw_measurements = [
            m
            for m in self.measurements_until(at)
            if m["region"] == region and m["cap"] == cap and int(m["route_seq"]) == seq
        ]
        start = int(first_receipt["at_ms"]) if first_receipt else None
        end = int(next_receipt["at_ms"]) if next_receipt else None
        end = int(next_receipt["at_ms"]) if next_receipt else None
        used: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for m in raw_measurements:
            reasons = []
            if not receipt:
                reasons.append("no_route_applied_receipt")
            else:
                if int(m["from_ms"]) < start:
                    reasons.append("before_actual_effect_start")
                if end is not None and int(m["to_ms"]) > end:
                    reasons.append("crosses_or_after_superseded_route")
            item = {k: m.get(k) for k in ("_id", "from_ms", "to_ms", "served_build", "total", "errors", "dep", "arrived_ms")}
            item["attribution_reasons"] = reasons
            (excluded if reasons else used).append(item)
        agg: dict[str, dict[str, dict[str, int]]] = {}
        for m in used:
            dep = m["dep"]
            build = m["served_build"]
            bucket = agg.setdefault(dep, {}).setdefault(build, {"total": 0, "errors": 0})
            bucket["total"] += int(m["total"])
            bucket["errors"] += int(m["errors"])
        policy = self.policy_for(region, cap)
        target = spec.get("build")
        baseline = spec.get("baseline")
        healthy = agg.get("healthy", {})
        candidate = dict(healthy.get(target, {"total": 0, "errors": 0})) if target else {"total": 0, "errors": 0}
        control = dict(healthy.get(baseline, {"total": 0, "errors": 0})) if baseline else {"total": 0, "errors": 0}
        candidate["rate"] = error_rate(candidate)
        control["rate"] = error_rate(control)
        effect = excess_pp(candidate, control) if candidate["total"] and control["total"] else None
        min_each = int(policy["min_each"]) if policy else None
        suff = {
            "candidate": {"have": candidate["total"], "required": min_each, "missing": max(0, min_each - candidate["total"]) if min_each is not None else None},
            "control": {"have": control["total"], "required": min_each, "missing": max(0, min_each - control["total"]) if min_each is not None else None},
        }
        sufficient = bool(
            policy and candidate["total"] >= policy["min_each"] and control["total"] >= policy["min_each"]
        )
        # Detect simultaneous impaired intervals for both served versions.
        impaired_pairs = []
        impaired_target = [m for m in used if m["dep"] == "impaired" and m["served_build"] == target]
        impaired_control = [m for m in used if m["dep"] == "impaired" and m["served_build"] == baseline]
        for a in impaired_target:
            for b in impaired_control:
                if overlap(a["from_ms"], a["to_ms"], b["from_ms"], b["to_ms"]):
                    impaired_pairs.append({"candidate": a["_id"], "control": b["_id"], "interval": [max(a["from_ms"], b["from_ms"]), min(a["to_ms"], b["to_ms"])]})
        unknown = [m["_id"] for m in used if m["dep"] == "unknown"]
        impaired = [m["_id"] for m in used if m["dep"] == "impaired"]
        findings: list[str] = []
        supported = False
        verdict = "insufficient_evidence"
        if not receipt:
            verdict = "awaiting_route_effect"
            findings.append("只有请求，没有对应 seq 的 route_applied 回执；不能把请求时间当成生效时间")
        elif not policy:
            verdict = "no_policy"
            findings.append("缺少观察策略，无法判定样本下限和错误率阈值")
        elif impaired_pairs:
            verdict = "shared_dependency_incident"
            findings.append("候选与对照在重叠区间同时受共享依赖故障影响；触发暂停，既不能记为候选独有回归，也不能证明健康")
        elif sufficient and effect is not None and effect > float(policy["max_excess_pp"]):
            verdict = "regression"
            findings.append(f"健康区间错误率差 {effect}pp，超过阈值 {policy['max_excess_pp']}pp")
        elif sufficient and effect is not None:
            verdict = "supported"
            supported = True
            findings.append(f"两组样本均达到 {policy['min_each']}，错误率差 {effect}pp <= {policy['max_excess_pp']}pp")
        else:
            missing_parts = []
            if suff["candidate"]["missing"]:
                missing_parts.append(f"候选 {target} 还缺 {suff['candidate']['missing']} 个健康样本")
            if suff["control"]["missing"]:
                missing_parts.append(f"对照 {baseline} 还缺 {suff['control']['missing']} 个健康样本")
            verdict = "waiting_for_samples"
            findings.append("；".join(missing_parts) or "样本尚不足以判定")
        if unknown:
            findings.append("dep=unknown 区间已排除，不能视为 healthy：" + ", ".join(unknown))
        if impaired and not impaired_pairs:
            findings.append("存在依赖故障区间，已排除：" + ", ".join(impaired))
        window = None
        if used:
            window = [min(m["from_ms"] for m in used), max(m["to_ms"] for m in used)]
        return {
            "seq": seq,
            "request_kind": request["kind"],
            "requested_at_ms": request["at_ms"],
            "requested_value": request.get("value"),
            "receipt": receipt,
            "first_receipt": first_receipt,
            "receipts": receipts,
            "conflicting_receipt_values": sorted(v for v in receipt_values if v is not None) if len(receipt_values) > 1 else [],
            "actual_percent": receipt.get("value") if receipt else None,
            "effective_from_ms": start,
            "effective_to_ms": end,
            "policy_window_ms": policy.get("window_ms") if policy else None,
            "observation_window": window,
            "aggregated": agg,
            "candidate": candidate,
            "control": control,
            "effect_pp": effect,
            "sufficiency": suff,
            "sufficient": sufficient,
            "shared_dependency_incident": bool(impaired_pairs),
            "impaired_pairs": impaired_pairs,
            "unknown_records": unknown,
            "impaired_records": impaired,
            "used_measurements": used,
            "excluded_measurements": excluded,
            "verdict": verdict,
            "supported": supported,
            "findings": findings,
        }

    # ---------------------------------------------------------- rollback logic

    def rollback_readiness(self, release: str, at: int | None = None, *, _compute_completed: bool = True) -> dict[str, Any]:
        at = self.view_at(at)
        spec = self.resolve_spec(release, at)
        region, cap = spec.get("region"), spec.get("cap")
        if not region or not cap:
            return {"complete": False, "status": "missing_scope", "blockers": ["缺少 region/cap"]}
        requests = self.release_requests(release, at)
        rollbacks = [r for r in requests if r["kind"] == "rollback"]
        if not rollbacks:
            return {"complete": False, "status": "no_rollback_request", "blockers": ["尚未记录回撤请求"]}
        rollback = rollbacks[-1]
        seq = int(rollback["seq"])
        receipts = [r for r in self.route_receipts(region, cap, at) if int(r["seq"]) == seq]
        receipt = receipts[-1] if receipts else None
        current = self.current_route(region, cap, at)
        later_routes = []
        if receipt:
            later_routes = [
                r for r in self.route_receipts(region, cap, at)
                if int(r["seq"]) > seq
            ]
        blockers: list[str] = []
        if not receipt:
            blockers.append(f"回撤代次 seq={seq} 尚未收到 route_applied 回执")
        elif float(receipt.get("value")) != 0:
            blockers.append(f"回撤回执实际候选流量为 {receipt.get('value')}%，不是 0%")
        # A later generation does not erase whether this applicable rollback
        # completed. It is reported separately because a new release may have
        # safely started another expansion after the completion point.
        reintroduced = bool(current and receipt and int(current["seq"]) > seq and float(current.get("value") or 0) > 0)
        baseline = spec.get("baseline")
        if not baseline:
            blockers.append("缺少明确的旧稳定 baseline 构建，无法证明旧流量恢复")
        effective_at = int(receipts[0]["at_ms"]) if receipts else None
        old_traffic = {"total": 0, "errors": 0, "successful": 0, "healthy_successful": 0}
        old_records: list[dict[str, Any]] = []
        if receipt and baseline:
            for m in self.measurements_until(at):
                if (
                    m["region"] == region
                    and m["cap"] == cap
                    and int(m["route_seq"]) == seq
                    and m["served_build"] == baseline
                    and effective_at is not None
                    and int(m["from_ms"]) >= effective_at
                ):
                    successful = int(m["total"] - m["errors"])
                    old_traffic["total"] += int(m["total"])
                    old_traffic["errors"] += int(m["errors"])
                    old_traffic["successful"] += successful
                    if m.get("dep") == "healthy":
                        old_traffic["healthy_successful"] += successful
                    old_records.append(m["_id"])
            if old_traffic["successful"] <= 0:
                blockers.append("回撤生效后尚未观察到旧版本成功通行样本")
        jobs = self.jobs_snapshot(at=at, region=region)
        relevant_jobs = []
        for job in jobs.values():
            if not job.get("unfinished"):
                continue
            # All unfinished regional work is checked because queue formats are
            # not identical to the routing client_cap label.
            relevant_jobs.append(job["id"])
            if job.get("destination_unresolved"):
                blockers.append(f"作业 {job['id']} 仍无可靠兼容归宿或存在互斥领取风险")
        freezes = [f for f in self.active_freezes(at) if f["region"] == region and f["cap"] == cap]
        complete = not blockers
        return {
            "complete": complete,
            "status": "complete" if complete else "pending",
            "rollback_seq": seq,
            "rollback_requested_at_ms": rollback["at_ms"],
            "route_receipt": receipt,
            "current_route": current,
            "later_routes": later_routes,
            "candidate_reintroduced_by_later_route": reintroduced,
            "old_build": baseline,
            "old_traffic": old_traffic,
            "old_traffic_measurement_ids": old_records,
            "unfinished_jobs": sorted(relevant_jobs),
            "active_freezes": freezes,
            "blockers": blockers,
            "completed_at_ms": self._rollback_completed_at(release) if complete and _compute_completed else None,
        }

    def _rollback_completed_at(self, release: str) -> int | None:
        """Find first logical time at which all completion gates were known."""
        times = [0, *[int(e["at_ms"]) for e in self.all_observations()], *[int(m["arrived_ms"]) for m in self.all_measurements()]]
        for t in sorted(set(times)):
            if self.rollback_readiness(release, at=t, _compute_completed=False).get("complete"):
                return t
        return None

    # --------------------------------------------------------------- actions

    def safe_actions(self, release: str | None = None, at: int | None = None) -> list[dict[str, Any]]:
        at = self.view_at(at)
        actions: list[dict[str, Any]] = []
        releases = [release] if release else self.known_releases(at)
        for rel in releases:
            spec = self.resolve_spec(rel, at)
            if not spec.get("region") or not spec.get("cap"):
                continue
            region, cap = spec["region"], spec["cap"]
            requests = self.release_requests(rel, at)
            latest = requests[-1] if requests else None
            readiness = self.rollback_readiness(rel, at)
            preflight = self.proposal_preflight(region, cap, spec.get("build"), at, release=rel)
            fence = preflight.get("control_fence") or {}
            if fence.get("rollback_outstanding"):
                pending = fence.get("rollback_readiness") or readiness
                actions.append({"release": rel, "severity": "critical", "code": "COMPLETE_ROLLBACK_VERIFICATION", "summary": "；".join(pending.get("blockers") or ["回撤尚未闭环"]), "command": f"python release_console.py show {rel} --at {at}"})
                continue
            if latest and latest["kind"] == "rollback":
                if readiness["complete"]:
                    if readiness.get("candidate_reintroduced_by_later_route"):
                        current = readiness["current_route"]
                        actions.append({"release": rel, "severity": "warning", "code": "ROLLBACK_COMPLETE_LATER_EXPANSION", "summary": f"seq={readiness['rollback_seq']} 的回撤已闭环（{readiness['completed_at_ms']}ms）；当前 seq={current['seq']} 又有 {current.get('value')}% 候选流量，由更新的发布单跟踪，晚到确认不得替代证据门", "command": f"python release_console.py show {rel} && python release_console.py actions"})
                    else:
                        actions.append({"release": rel, "severity": "ok", "code": "ROLLBACK_COMPLETE", "summary": "路由已回撤、旧流量恢复、遗留任务有可靠归宿；可关闭回撤", "command": None})
                else:
                    actions.append({"release": rel, "severity": "critical", "code": "COMPLETE_ROLLBACK_VERIFICATION", "summary": "；".join(readiness["blockers"]), "command": f"python release_console.py show {rel} --at {at}"})
                continue
            if preflight["matching_freezes"]:
                actions.append({"release": rel, "severity": "critical", "code": "MAINTAIN_ACCEPTANCE_FREEZE", "summary": preflight["matching_freezes"][0]["reason"], "command": "python release_console.py actions"})
                actions.append({"release": rel, "severity": "critical", "code": "RESTORE_COMPATIBLE_CAPACITY", "summary": "恢复兼容处理实例或转换任务格式；在此之前不得扩容", "command": "python release_console.py instance --instance <id> --region " + region + " --build <compatible-build> --at <ms>"})
                actions.append({"release": rel, "severity": "critical", "code": "ROLLBACK", "summary": "若候选仍在承接流量，发出回撤；晚到回执不得作为继续放量许可", "command": f"python release_console.py rollback {rel} --at {at}"})
                continue
            if not preflight["target_verification"]["verified"]:
                actions.append({"release": rel, "severity": "high", "code": "VERIFY_TARGET_BUILD", "summary": "；".join(preflight["target_verification"]["issues"]), "command": f"python release_console.py show {rel} --at {at}"})
                continue
            if fence.get("pause_active") or (latest and latest["kind"] == "pause"):
                actions.append({"release": rel, "severity": "warning", "code": "REMAIN_PAUSED", "summary": "暂停围栏有效；只继续校验已生效路由和证据，重复发布点击不会恢复推进", "command": f"python release_console.py show {rel} --at {at}"})
                continue
            evidence = self.latest_round_evidence(rel, at)
            if evidence is None:
                actions.append({"release": rel, "severity": "info", "code": "RECORD_OR_WAIT_ROUTE", "summary": "尚未形成可评估轮次；等待 route_applied 回执", "command": "python release_console.py report"})
            elif evidence["verdict"] in {"waiting_for_samples", "awaiting_route_effect"}:
                missing = "；".join(evidence["findings"])
                actions.append({"release": rel, "severity": "warning", "code": "CONTINUE_WAIT", "summary": missing, "command": f"python release_console.py wait --to <later-ms> && python release_console.py show {rel}"})
            elif evidence["verdict"] == "shared_dependency_incident":
                actions.append({"release": rel, "severity": "critical", "code": "PAUSE_FOR_SHARED_DEPENDENCY", "summary": evidence["findings"][0], "command": f"python release_console.py pause {rel} --at {at}"})
            elif evidence["verdict"] == "regression":
                actions.append({"release": rel, "severity": "critical", "code": "ROLLBACK", "summary": evidence["findings"][0], "command": f"python release_console.py rollback {rel} --at {at}"})
            elif evidence["verdict"] == "supported":
                actions.append({"release": rel, "severity": "ok", "code": "HOLD_VALIDATED_SCOPE", "summary": f"证据仅支持 {region}/{cap} seq={evidence['seq']} 的 {evidence['actual_percent']}% 稳定范围；下一批需新申请", "command": f"python release_console.py audit {rel}"})
            else:
                actions.append({"release": rel, "severity": "warning", "code": "DO_NOT_EXPAND", "summary": "；".join(evidence["findings"]), "command": f"python release_console.py show {rel}"})
        if not release:
            for freeze in self.active_freezes(at):
                jobs = ", ".join(freeze.get("waiting_jobs", [])) or "见作业面板"
                actions.append({"release": None, "severity": "critical", "code": "ONCALL_WAITING_WORK", "summary": f"{freeze['region']}/{freeze['cap']} {freeze.get('format')} 等待作业：{jobs}", "command": "python release_console.py jobs --waiting"})
        return actions

    def known_releases(self, at: int | None = None) -> list[str]:
        at = self.view_at(at)
        ids = {
            e["item"]
            for e in self.observations_until(at)
            if e.get("kind") in REQUEST_KINDS and e.get("item")
        }
        ids.update(self.state["specs"].keys())
        return sorted(ids, key=lambda x: version_key(x))

    def latest_round_evidence(self, release: str, at: int) -> dict[str, Any] | None:
        spec = self.resolve_spec(release, at)
        requests = self.release_requests(release, at)
        proposes = [r for r in requests if r["kind"] == "propose"]
        if not proposes:
            # Pause/rollback-only state still has a latest request.
            if not requests:
                return None
            candidate = requests[-1]
        else:
            candidate = proposes[-1]
        return self.round_evidence(candidate, spec, at)

    # --------------------------------------------------------------- reports

    def release_report(self, release: str, at: int | None = None) -> dict[str, Any]:
        at = self.view_at(at)
        spec = self.resolve_spec(release, at)
        requests = self.release_requests(release, at)
        rounds = [self.round_evidence(req, spec, at) for req in requests]
        verification = self.verify_target(spec.get("region"), spec.get("build"), at) if spec.get("region") else None
        compat = None
        if spec.get("region") and spec.get("cap"):
            compat = self.compatibility_matrix(spec["region"], spec["cap"], spec.get("build"), at)
        rollback = self.rollback_readiness(release, at)
        # Queue formats are not identical to routing client_cap; retain the
        # whole regional job ledger for audit and compatibility tracing.
        relevant_jobs = list(self.jobs_snapshot(at=at, region=spec.get("region")).values()) if spec.get("region") else []
        preflight = self.proposal_preflight(spec["region"], spec["cap"], spec.get("build"), at, release=release) if spec.get("region") and spec.get("cap") else None
        stable_scopes = []
        for r in rounds:
            # Pause/rollback generations are control generations, not validated
            # canary advancement scopes, even if old-build counts look healthy.
            if r["supported"] and r["request_kind"] == "propose":
                stable_scopes.append(
                    {
                        "release": release,
                        "region": spec["region"],
                        "client_cap": spec["cap"],
                        "route_seq": r["seq"],
                        "actual_percent": r["actual_percent"],
                        "effective_from_ms": r["effective_from_ms"],
                        "effective_to_ms": r["effective_to_ms"],
                        "observation_window": r["observation_window"],
                        "measurement_ids": [m["_id"] for m in r["used_measurements"]],
                        "supported_boundary": "仅该 region/client_cap/route_seq/实际版本范围；不得外推到其他地区、能力或更高比例",
                    }
                )
        return {
            "release": release,
            "at_ms": at,
            "spec": spec,
            "target_verification": verification,
            "compatibility": compat,
            "requests": requests,
            "rounds": rounds,
            "stable_scopes": stable_scopes,
            "rollback": rollback,
            "jobs": relevant_jobs,
            "freezes": [f for f in self.active_freezes(at) if f.get("region") == spec.get("region")],
            "preflight": preflight,
            "safe_actions": self.safe_actions(release, at),
        }

    def dashboard(self, at: int | None = None) -> dict[str, Any]:
        at = self.view_at(at)
        releases = [self.release_report(r, at) for r in self.known_releases(at)]
        jobs = self.jobs_snapshot(at=at)
        waiting = [j for j in jobs.values() if j.get("waiting") or j.get("destination_unresolved")]
        return {
            "at_ms": at,
            "materials_dir": str(self.materials_dir),
            "releases": releases,
            "freezes": self.active_freezes(at),
            "waiting_or_blocked_jobs": waiting,
            "safe_actions": self.safe_actions(at=at),
        }

    # --------------------------------------------------------------- rendering

    def render_dashboard(self, at: int | None = None) -> str:
        data = self.dashboard(at)
        lines = [f"发布控制台  logical_time={data['at_ms']}ms", f"资料目录：{data['materials_dir']}", ""]
        if not data["releases"]:
            lines.append("（当前时间点没有发布申请）")
        for rel in data["releases"]:
            spec = rel["spec"]
            rb = rel["rollback"]
            tv = rel.get("target_verification") or {}
            fence = (rel.get("preflight") or {}).get("control_fence") or {}
            if rb.get("complete"):
                status = "ROLLBACK_COMPLETE"
                if rb.get("candidate_reintroduced_by_later_route"):
                    status += "_LATER_EXPANSION_PRESENT"
            elif rb.get("status") == "pending" or fence.get("rollback_outstanding"):
                status = "ROLLBACK_PENDING"
            elif fence.get("pause_active"):
                status = "PAUSED"
            elif tv and not tv.get("verified"):
                status = "TARGET_BLOCKED"
            else:
                latest_round = rel["rounds"][-1] if rel["rounds"] else None
                status = latest_round["verdict"].upper() if latest_round else "NO_REQUEST"
            lines.append(f"● {rel['release']}  {spec.get('region')}/{spec.get('cap')}  target={spec.get('build')} baseline={spec.get('baseline')}")
            lines.append(f"  状态：{status}")
            tv = rel.get("target_verification") or {}
            if tv:
                lines.append("  实例：" + ("; ".join(tv.get("target_instances", [])) + f" 实际加载 {tv.get('target')}，已核验" if tv.get("verified") else "；".join(tv.get("issues", []))))
            for scope in rel.get("stable_scopes", []):
                lines.append(f"  可追溯稳定范围：seq={scope['route_seq']} actual={scope['actual_percent']}% effect=[{scope['effective_from_ms']},{scope['effective_to_ms']}) observations={','.join(scope['measurement_ids'])}")
            if rb.get("status") == "pending":
                lines.append("  回撤未完成：" + "；".join(rb["blockers"]))
            lines.append("")
        lines.append("冻结范围：")
        if data["freezes"]:
            for f in data["freezes"]:
                jobs = ",".join(f.get("waiting_jobs", [])) or "-"
                lines.append(f"  ! {f['region']}/{f['cap']} build={f.get('build')} format={f.get('format')} waiting={jobs} :: {f['reason']}")
        else:
            lines.append("  无")
        lines.append("")
        lines.append("等待/受阻作业：")
        if data["waiting_or_blocked_jobs"]:
            for j in data["waiting_or_blocked_jobs"]:
                dest = []
                for c in j.get("destination_checks", []):
                    dest.append(f"seq{c['seq']}:{c['status']}/{c['format']} -> {[d['build'] for d in c.get('reliable_destinations', [])]}")
                lines.append(f"  ! {j['id']} status={j['status']} " + " | ".join(dest))
        else:
            lines.append("  无")
        lines.append("")
        lines.append("当前安全动作：")
        for a in data["safe_actions"]:
            prefix = {"critical": "✗", "high": "!", "warning": "?", "ok": "✓", "info": "·"}.get(a["severity"], "·")
            scope = f"[{a['release']}] " if a.get("release") else ""
            lines.append(f"  {prefix} {scope}{a['code']}: {a['summary']}")
            if a.get("command"):
                lines.append(f"      $ {a['command']}")
        return "\n".join(lines)

    def render_release(self, release: str, at: int | None = None) -> str:
        report = self.release_report(release, at)
        lines = [self.render_dashboard(at).split("\n\n")[0], ""]
        spec = report["spec"]
        lines.append(f"发布 {release} @ {report['at_ms']}ms")
        lines.append(f"范围={spec.get('region')}/{spec.get('cap')} target={spec.get('build')} baseline={spec.get('baseline')} desired={spec.get('desired_percent')}%")
        tv = report["target_verification"]
        if tv:
            lines.append("实例核验：" + ("通过 " + ",".join(tv.get("target_instances", [])) if tv["verified"] else "未通过 — " + "；".join(tv["issues"])))
        compat = report.get("compatibility")
        if compat:
            lines.append("能力/队列兼容：")
            for row in compat["rows"]:
                dest = [d["build"] + ("?" if d["confidence"] != "instance_receipt" else "") for d in row["reliable_destinations"]]
                lines.append(
                    f"  build={row['build']} cap={row['route_client_cap']} emits={row['emitted_format']} "
                    f"queue_manifest={row['manifest_claimable_builds']} reliable={dest} old_can_continue={row['old_handler_can_continue']}"
                )
        lines.append("")
        lines.append("路由与证据轮次：")
        for r in report["rounds"]:
            lines.append(
                f"  seq={r['seq']} {r['request_kind']} requested@{r['requested_at_ms']} value={r['requested_value']} "
                f"receipt={'yes@'+str(r['receipt']['at_ms'])+' actual='+str(r['actual_percent'])+'%' if r['receipt'] else 'NO'}"
            )
            lines.append(f"    verdict={r['verdict']} effect_pp={r['effect_pp']} suff={r['sufficiency']}")
            for finding in r["findings"]:
                lines.append(f"    - {finding}")
            for m in r["used_measurements"]:
                lines.append(f"    + use {m['_id']} [{m['from_ms']},{m['to_ms']}) {m['served_build']} n={m['total']} err={m['errors']} dep={m['dep']}")
            for m in r["excluded_measurements"]:
                lines.append(f"    - skip {m['_id']} [{m['from_ms']},{m['to_ms']}) {m['served_build']} :: {','.join(m['attribution_reasons'])}")
        lines.append("")
        rb = report["rollback"]
        lines.append("回撤闭环：")
        lines.append(f"  complete={rb['complete']} status={rb['status']} old_traffic={rb.get('old_traffic')}")
        for blocker in rb.get("blockers", []):
            lines.append(f"  blocker: {blocker}")
        if rb.get("completed_at_ms") is not None:
            lines.append(f"  first_completed_at={rb['completed_at_ms']}ms")
        lines.append("")
        lines.append("相关作业：")
        if not report["jobs"]:
            lines.append("  无")
        for job in report["jobs"]:
            lines.append(f"  {job['id']} status={job['status']} unfinished={job['unfinished']} unresolved={job['destination_unresolved']}")
            for check in job["destination_checks"]:
                lines.append(f"    seq={check['seq']} {check['status']} format={check['format']} holder={check.get('by_build')} destinations={[d['build'] for d in check['reliable_destinations']]}")
        lines.append("")
        lines.append("安全动作：")
        for a in report["safe_actions"]:
            lines.append(f"  [{a['severity']}] {a['code']}: {a['summary']}")
            if a.get("command"):
                lines.append(f"      $ {a['command']}")
        return "\n".join(lines)


# --------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default=argparse.SUPPRESS, help="本地 JSON 状态路径")
    common.add_argument("--materials-dir", default=argparse.SUPPRESS, help="资料目录")
    common.add_argument("--at", type=int, default=argparse.SUPPRESS, help="按逻辑/到达时间回放，只看 <=AT 的记录")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON")

    parser = argparse.ArgumentParser(description="本地发布控制台（标准库，资料只读，状态可持久化）")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="本地 JSON 状态路径")
    parser.add_argument("--materials-dir", default=str(DEFAULT_MATERIALS), help="资料目录")
    parser.add_argument("--at", type=int, default=None, help="按逻辑/到达时间回放，只看 <=AT 的记录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    sub = parser.add_subparsers(dest="command")

    def sp(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kwargs)

    sp("report", help="总览所有发布")
    show = sp("show", help="查看单次发布详情")
    show.add_argument("release")
    audit = sp("audit", help="输出可回查的 JSON 审计档案")
    audit.add_argument("release")
    actions = sp("actions", help="只列当前安全动作")
    actions.add_argument("release", nargs="?")
    jobs = sp("jobs", help="查看作业；--waiting 只列等待/受阻")
    jobs.add_argument("--waiting", action="store_true")

    wait = sp("wait", help="把逻辑观察时间推进到 T 或推进 MS")
    wait.add_argument("--to", type=int)
    wait.add_argument("--ms", type=int)

    propose = sp("propose", help="记录发布申请/放量请求")
    propose.add_argument("release")
    propose.add_argument("--region", required=True)
    propose.add_argument("--cap", required=True)
    propose.add_argument("--percent", type=float, required=True)
    propose.add_argument("--build")
    propose.add_argument("--baseline")
    propose.add_argument("--seq", type=int)
    propose.add_argument("--request-id")
    propose.add_argument("--idem-key")

    pause = sp("pause", help="记录暂停请求")
    pause.add_argument("release")
    pause.add_argument("--seq", type=int)
    pause.add_argument("--request-id")
    pause.add_argument("--idem-key")
    resume = sp("resume", help="显式解除暂停围栏；仍需重新过证据/预检")
    resume.add_argument("release")
    resume.add_argument("--seq", type=int)
    resume.add_argument("--request-id")
    resume.add_argument("--idem-key")
    rollback = sp("rollback", help="记录回撤请求")
    rollback.add_argument("release")
    rollback.add_argument("--seq", type=int)
    rollback.add_argument("--request-id")
    rollback.add_argument("--idem-key")

    applied = sp("applied", help="录入 route_applied 实际生效回执")
    applied.add_argument("--seq", type=int, required=True)
    applied.add_argument("--region", required=True)
    applied.add_argument("--cap", required=True)
    applied.add_argument("--percent", type=float, required=True)
    applied.add_argument("--item", default="local-route")
    applied.add_argument("--idem-key")

    instance = sp("instance", help="录入实例加载回执")
    instance.add_argument("--instance", required=True)
    instance.add_argument("--region", required=True)
    instance.add_argument("--build")
    instance.add_argument("--offline", action="store_true")
    instance.add_argument("--seq", type=int, default=1)

    claim = sp("claim", help="录入领取回执；不安全的版本/格式或重复领取会被拒绝")
    claim.add_argument("--job", required=True)
    claim.add_argument("--region", required=True)
    claim.add_argument("--cap", required=True)
    claim.add_argument("--seq", type=int, required=True)
    claim.add_argument("--format", required=True)
    claim.add_argument("--by-build", required=True)
    claim.add_argument("--allow-unsafe-source", action="store_true", help=argparse.SUPPRESS)

    ended = sp("claim-ended", help="记录租约结束但任务未完成")
    ended.add_argument("--job", required=True)
    ended.add_argument("--seq", type=int, required=True)
    ended.add_argument("--reason")
    finished = sp("finished", help="记录作业完成")
    finished.add_argument("--job", required=True)
    finished.add_argument("--seq", type=int, required=True)

    freeze = sp("freeze", help="手工冻结接纳范围")
    freeze.add_argument("--region", required=True)
    freeze.add_argument("--cap", required=True)
    freeze.add_argument("--build")
    freeze.add_argument("--format")
    freeze.add_argument("--reason", required=True)
    unfreeze = sp("unfreeze", help="关闭匹配的手工冻结；派生冻结在容量恢复后自动消失")
    unfreeze.add_argument("--region", required=True)
    unfreeze.add_argument("--cap", required=True)
    unfreeze.add_argument("--build")
    unfreeze.add_argument("--format")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        console = ReleaseConsole(materials_dir=args.materials_dir, state_path=args.state)
        command = args.command or "report"
        if command == "report":
            data = console.dashboard(args.at)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            else:
                print(console.render_dashboard(args.at))
        elif command == "show":
            if args.json:
                print(json.dumps(console.release_report(args.release, args.at), ensure_ascii=False, indent=2, default=str))
            else:
                print(console.render_release(args.release, args.at))
        elif command == "audit":
            print(json.dumps(console.release_report(args.release, args.at), ensure_ascii=False, indent=2, default=str))
        elif command == "actions":
            data = console.safe_actions(args.release, args.at)
            print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else "\n".join(f"[{a['severity']}] {a['release'] or '-'} {a['code']}: {a['summary']}" for a in data))
        elif command == "jobs":
            jobs = list(console.jobs_snapshot(args.at).values())
            if args.waiting:
                jobs = [j for j in jobs if j.get("waiting") or j.get("destination_unresolved")]
            print(json.dumps(jobs, ensure_ascii=False, indent=2, default=str) if args.json else "\n".join(f"{j['id']} {j['status']} unresolved={j['destination_unresolved']}" for j in jobs) or "无")
        elif command == "wait":
            if args.to is None and args.ms is None:
                raise ConsoleError("wait 需要 --to MS 或 --ms N")
            data = console.wait_to(args.to) if args.to is not None else console.advance_wait(args.ms)
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str) if args.json else console.render_dashboard(data["at_ms"]))
        elif command == "propose":
            result = console.propose(
                args.release, region=args.region, cap=args.cap, percent=args.percent, build=args.build,
                baseline=args.baseline, at=args.at, seq=args.seq, request_id=args.request_id, idem_key=args.idem_key,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result["preflight"]["safe_to_issue_route"] else 2
        elif command == "pause":
            print(json.dumps(console.control_request("pause", args.release, at=args.at, seq=args.seq, request_id=args.request_id, idem_key=args.idem_key), ensure_ascii=False, indent=2))
        elif command == "resume":
            print(json.dumps(console.control_request("resume", args.release, at=args.at, seq=args.seq, request_id=args.request_id, idem_key=args.idem_key), ensure_ascii=False, indent=2))
        elif command == "rollback":
            print(json.dumps(console.control_request("rollback", args.release, at=args.at, seq=args.seq, request_id=args.request_id, idem_key=args.idem_key), ensure_ascii=False, indent=2))
        elif command == "applied":
            print(json.dumps(console.record_route_applied(seq=args.seq, region=args.region, cap=args.cap, percent=args.percent, at=args.at, item=args.item, idem_key=args.idem_key), ensure_ascii=False, indent=2))
        elif command == "instance":
            print(json.dumps(console.record_instance(instance=args.instance, region=args.region, build=args.build, offline=args.offline, at=args.at, seq=args.seq), ensure_ascii=False, indent=2))
        elif command == "claim":
            result = console.record_claim(job=args.job, region=args.region, cap=args.cap, seq=args.seq, data_format=args.format, by_build=args.by_build, at=args.at, enforce_safety=not args.allow_unsafe_source)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif command in {"claim-ended", "finished"}:
            kind = "claim_ended" if command == "claim-ended" else "finished"
            print(json.dumps(console.record_job_terminal(kind, job=args.job, seq=args.seq, reason=getattr(args, "reason", None), at=args.at), ensure_ascii=False, indent=2))
        elif command == "freeze":
            print(json.dumps(console.freeze_scope(region=args.region, cap=args.cap, candidate_build=args.build, data_format=args.format, reason=args.reason, at=args.at), ensure_ascii=False, indent=2))
        elif command == "unfreeze":
            print(json.dumps(console.unfreeze_scope(region=args.region, cap=args.cap, candidate_build=args.build, data_format=args.format, at=args.at), ensure_ascii=False, indent=2))
        else:
            parser.error(f"未知命令：{command}")
        return 0
    except SafetyError as exc:
        print(f"安全拦截：{exc}", file=sys.stderr)
        return 3
    except ConsoleError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
