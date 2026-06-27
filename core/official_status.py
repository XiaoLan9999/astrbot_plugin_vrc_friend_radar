from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("vrc_friend_radar")

from .config import DEFAULT_OFFICIAL_STATUS_SUMMARY_URL, PluginConfig

if TYPE_CHECKING:
    from .repository import SettingsRepository


COMPONENT_STATUS_LABELS = {
    "operational": "正常",
    "degraded_performance": "性能下降/卡顿",
    "partial_outage": "部分故障",
    "major_outage": "严重故障",
    "under_maintenance": "维护中",
}

INDICATOR_LABELS = {
    "none": "正常",
    "minor": "轻微异常",
    "major": "部分故障",
    "critical": "严重故障",
    "maintenance": "维护中",
}

INCIDENT_STATUS_LABELS = {
    "investigating": "调查中",
    "identified": "已定位",
    "monitoring": "观察中",
    "resolved": "已恢复",
    "scheduled": "已计划",
    "in_progress": "进行中",
    "verifying": "验证中",
    "completed": "已完成",
}

ACTIVE_INCIDENT_DONE_STATUSES = {"resolved", "postmortem", "completed"}
ACTIVE_MAINTENANCE_DONE_STATUSES = {"completed"}
DEFAULT_OFFICIAL_STATUS_STATUS_URL = "https://status.vrchat.com/api/v2/status.json"


class OfficialStatusFetchError(RuntimeError):
    pass


def _to_text(value: Any) -> str:
    return str(value or "").strip()


def describe_official_status_exception(exc: Exception) -> str:
    if isinstance(exc, OfficialStatusFetchError):
        return str(exc) or "官方状态 API 请求失败"
    name = type(exc).__name__
    text = str(exc or "").strip()
    if isinstance(
        exc,
        (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            TimeoutError,
        ),
    ):
        return f"{name}: 请求超时"
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return f"{name}: HTTP {response.status_code} {response.reason_phrase}"
    if isinstance(exc, httpx.RequestError):
        return f"{name}: {text or repr(exc)}"
    return f"{name}: {text or repr(exc)}"


def _label(raw: str, mapping: dict[str, str]) -> str:
    key = _to_text(raw).lower()
    if not key:
        return "未知"
    text = mapping.get(key)
    return f"{text}({key})" if text else key


@dataclass(slots=True)
class OfficialStatusSnapshot:
    indicator: str = ""
    description: str = ""
    page_url: str = "https://status.vrchat.com"
    updated_at: str = ""
    fetched_at: str = ""
    components: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    scheduled_maintenances: list[dict[str, Any]] = field(default_factory=list)

    def degraded_components(self) -> list[dict[str, Any]]:
        degraded = [
            item
            for item in self.components
            if _to_text(item.get("status")).lower() not in {"", "operational"}
        ]
        non_group = [item for item in degraded if not bool(item.get("group"))]
        return non_group or degraded

    def active_incidents(self) -> list[dict[str, Any]]:
        result = []
        for item in self.incidents:
            status = _to_text(item.get("status")).lower()
            if status not in ACTIVE_INCIDENT_DONE_STATUSES:
                result.append(item)
        return result

    def active_maintenances(self) -> list[dict[str, Any]]:
        result = []
        for item in self.scheduled_maintenances:
            status = _to_text(item.get("status")).lower()
            if status not in ACTIVE_MAINTENANCE_DONE_STATUSES:
                result.append(item)
        return result

    def requires_attention(self) -> bool:
        indicator = _to_text(self.indicator).lower()
        return bool(
            indicator not in {"", "none"}
            or self.degraded_components()
            or self.active_incidents()
            or self.active_maintenances()
        )

    def signature(self) -> str:
        payload = {
            "indicator": _to_text(self.indicator).lower(),
            "description": _to_text(self.description),
            "components": [
                {
                    "id": _to_text(item.get("id")),
                    "name": _to_text(item.get("name")),
                    "status": _to_text(item.get("status")).lower(),
                }
                for item in self.components
            ],
            "incidents": [
                {
                    "id": _to_text(item.get("id")),
                    "name": _to_text(item.get("name")),
                    "impact": _to_text(item.get("impact")).lower(),
                    "status": _to_text(item.get("status")).lower(),
                    "updated_at": _to_text(item.get("updated_at")),
                }
                for item in self.incidents
            ],
            "scheduled_maintenances": [
                {
                    "id": _to_text(item.get("id")),
                    "name": _to_text(item.get("name")),
                    "impact": _to_text(item.get("impact")).lower(),
                    "status": _to_text(item.get("status")).lower(),
                    "scheduled_for": _to_text(item.get("scheduled_for")),
                    "scheduled_until": _to_text(item.get("scheduled_until")),
                    "updated_at": _to_text(item.get("updated_at")),
                }
                for item in self.scheduled_maintenances
            ],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_official_status_payload(payload: dict[str, Any], fetched_at: str | None = None) -> OfficialStatusSnapshot:
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    components = payload.get("components") if isinstance(payload.get("components"), list) else []
    incidents = payload.get("incidents") if isinstance(payload.get("incidents"), list) else []
    scheduled = payload.get("scheduled_maintenances") if isinstance(payload.get("scheduled_maintenances"), list) else []

    return OfficialStatusSnapshot(
        indicator=_to_text(status.get("indicator")),
        description=_to_text(status.get("description")),
        page_url=_to_text(page.get("url")) or "https://status.vrchat.com",
        updated_at=_to_text(page.get("updated_at")),
        fetched_at=fetched_at or datetime.now().isoformat(timespec="seconds"),
        components=[item for item in components if isinstance(item, dict)],
        incidents=[item for item in incidents if isinstance(item, dict)],
        scheduled_maintenances=[item for item in scheduled if isinstance(item, dict)],
    )


class OfficialStatusService:
    def __init__(self, cfg: PluginConfig, settings_repo: SettingsRepository):
        self.cfg = cfg
        self.settings_repo = settings_repo
        self._task: asyncio.Task | None = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._change_callback: Callable[[OfficialStatusSnapshot], Awaitable[None]] | None = None
        self._last_signature = ""
        self._last_attention_required = False
        self._last_snapshot: OfficialStatusSnapshot | None = None
        self._last_error = ""
        self._last_error_at = 0.0
        self._last_seen_raw_notify_groups = self._dedupe_clean_ids(
            self.cfg.read_official_status_notify_group_ids_from_raw()
        )

    def set_change_callback(
        self,
        callback: Callable[[OfficialStatusSnapshot], Awaitable[None]] | None,
    ) -> None:
        self._change_callback = callback

    @staticmethod
    def _dedupe_clean_ids(group_ids: list[str] | None) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for group_id in group_ids or []:
            value = str(group_id or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def get_effective_notify_groups(self) -> list[str]:
        raw_groups = self._dedupe_clean_ids(self.cfg.read_official_status_notify_group_ids_from_raw())
        runtime_groups = self._dedupe_clean_ids(self.cfg.official_status_notify_group_ids)
        repo_groups = self._dedupe_clean_ids(self.settings_repo.get_official_status_notify_groups())

        if raw_groups != self._last_seen_raw_notify_groups:
            self.settings_repo.set_official_status_notify_groups(raw_groups)
            self.cfg.sync_runtime_lists(official_status_notify_group_ids=raw_groups, write_back_raw=True)
            result = raw_groups
        elif repo_groups != runtime_groups:
            self.cfg.sync_runtime_lists(official_status_notify_group_ids=repo_groups, write_back_raw=True)
            result = repo_groups
        else:
            result = runtime_groups

        self._last_seen_raw_notify_groups = self._dedupe_clean_ids(
            self.cfg.read_official_status_notify_group_ids_from_raw()
        )
        return result

    def add_notify_group(self, group_id: str) -> list[str]:
        groups = self.settings_repo.add_official_status_notify_group(group_id)
        self.cfg.sync_runtime_lists(official_status_notify_group_ids=groups, write_back_raw=True)
        self._last_seen_raw_notify_groups = self._dedupe_clean_ids(
            self.cfg.read_official_status_notify_group_ids_from_raw()
        )
        return groups

    def remove_notify_group(self, group_id: str) -> list[str]:
        groups = self.settings_repo.remove_official_status_notify_group(group_id)
        self.cfg.sync_runtime_lists(official_status_notify_group_ids=groups, write_back_raw=True)
        self._last_seen_raw_notify_groups = self._dedupe_clean_ids(
            self.cfg.read_official_status_notify_group_ids_from_raw()
        )
        return groups

    def get_runtime_summary(self) -> str:
        groups = self.get_effective_notify_groups()
        last_status = "未查询"
        if self._last_snapshot:
            last_status = _label(self._last_snapshot.indicator, INDICATOR_LABELS)
        if self._last_error:
            last_status = f"最近失败：{self._last_error}"
        return (
            f"官方状态监控：{'开启' if self.cfg.enable_official_status_monitor else '关闭'}\n"
            f"轮询间隔：{self.cfg.official_status_poll_interval_seconds} 秒\n"
            f"播报群数：{len(groups)}\n"
            f"最近状态：{last_status}"
        )

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def get_last_snapshot(self) -> OfficialStatusSnapshot | None:
        return self._last_snapshot

    def _status_url_candidates(self) -> list[str]:
        candidates = [
            _to_text(self.cfg.official_status_summary_url),
            DEFAULT_OFFICIAL_STATUS_SUMMARY_URL,
            DEFAULT_OFFICIAL_STATUS_STATUS_URL,
        ]
        result: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if not item or item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    @staticmethod
    def _build_http_timeout(seconds: float) -> httpx.Timeout:
        seconds = max(0.5, float(seconds or 1.0))
        return httpx.Timeout(
            seconds,
            connect=min(1.5, seconds),
            read=seconds,
            write=seconds,
            pool=min(0.5, seconds),
        )

    async def fetch_once(self, timeout_seconds: float = 4.0) -> OfficialStatusSnapshot:
        headers = {
            "User-Agent": _to_text(self.cfg.vrchat_user_agent) or "AstrBotVRCFriendRadar/0.1.0",
            "Accept": "application/json",
        }
        timeout_seconds = max(1.0, min(float(timeout_seconds or 4.0), 15.0))
        deadline = time.monotonic() + timeout_seconds
        errors: list[str] = []

        async with httpx.AsyncClient(
            timeout=self._build_http_timeout(timeout_seconds),
            headers=headers,
            follow_redirects=True,
            trust_env=False,
            http2=False,
        ) as client:
            for url in self._status_url_candidates():
                remaining = deadline - time.monotonic()
                if remaining < 0.5:
                    errors.append(f"{url}: TimeoutError: 总查询时间预算已耗尽")
                    break
                try:
                    response = await client.get(
                        url,
                        timeout=self._build_http_timeout(min(2.5, remaining)),
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("官方状态 API 返回格式不是 JSON object")
                    snapshot = parse_official_status_payload(payload)
                    self._last_snapshot = snapshot
                    self._last_error = ""
                    self._last_error_at = 0.0
                    return snapshot
                except Exception as exc:
                    error_text = describe_official_status_exception(exc)
                    errors.append(f"{url}: {error_text}")
                    logger.warning(f"[vrc_friend_radar] 官方状态 API 请求失败 url={url} err={error_text}")

        detail = "；".join(errors) if errors else "没有可用的官方状态 API URL"
        raise OfficialStatusFetchError(detail)

    async def check_once(self, allow_notify: bool = True) -> OfficialStatusSnapshot:
        snapshot = await self.fetch_once()
        signature = snapshot.signature()
        attention = snapshot.requires_attention()
        first_check = not self._last_signature
        signature_changed = bool(self._last_signature and signature != self._last_signature)
        should_notify = False

        if allow_notify:
            if first_check and attention:
                should_notify = True
            elif signature_changed and (attention or self._last_attention_required):
                should_notify = True

        self._last_signature = signature
        self._last_attention_required = attention

        if should_notify and self._change_callback:
            try:
                await self._change_callback(snapshot)
            except Exception as exc:
                logger.error(f"[vrc_friend_radar] 官方状态变更回调失败: {exc}", exc_info=True)
        return snapshot

    async def _run_loop(self) -> None:
        while self._running:
            try:
                if self.cfg.enable_official_status_monitor:
                    await self.check_once(allow_notify=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = describe_official_status_exception(exc)
                self._last_error_at = time.time()
                logger.warning(f"[vrc_friend_radar] 官方状态监控查询失败: {self._last_error}")

            interval = max(120, int(self.cfg.official_status_poll_interval_seconds or 300))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def format_snapshot(self, snapshot: OfficialStatusSnapshot, title: str = "📡 VRChat 官方服务状态") -> str:
        lines = [title]
        desc = snapshot.description or "未知"
        lines.append(f"总体：{_label(snapshot.indicator, INDICATOR_LABELS)}（{desc}）")
        if snapshot.updated_at:
            lines.append(f"官方更新时间：{snapshot.updated_at}")
        lines.append(f"查询时间：{snapshot.fetched_at}")

        degraded = snapshot.degraded_components()
        if degraded:
            lines.append("异常组件：")
            for item in degraded[:8]:
                lines.append(f"- {_to_text(item.get('name')) or item.get('id')}: {_label(_to_text(item.get('status')), COMPONENT_STATUS_LABELS)}")
            if len(degraded) > 8:
                lines.append(f"- 其余 {len(degraded) - 8} 个组件略")
        else:
            lines.append("异常组件：无")

        incidents = snapshot.active_incidents()
        if incidents:
            lines.append("事件：")
            for item in incidents[:5]:
                impact = _to_text(item.get("impact")) or "unknown"
                status = _label(_to_text(item.get("status")), INCIDENT_STATUS_LABELS)
                name = _to_text(item.get("name")) or _to_text(item.get("id")) or "未命名事件"
                lines.append(f"- {name} | {impact} | {status}")
            if len(incidents) > 5:
                lines.append(f"- 其余 {len(incidents) - 5} 条事件略")
        else:
            lines.append("事件：无")

        maintenances = snapshot.active_maintenances()
        if maintenances:
            lines.append("维护：")
            for item in maintenances[:5]:
                status = _label(_to_text(item.get("status")), INCIDENT_STATUS_LABELS)
                name = _to_text(item.get("name")) or _to_text(item.get("id")) or "未命名维护"
                start = _to_text(item.get("scheduled_for"))
                end = _to_text(item.get("scheduled_until"))
                window = f" | {start} ~ {end}" if start or end else ""
                lines.append(f"- {name} | {status}{window}")
            if len(maintenances) > 5:
                lines.append(f"- 其余 {len(maintenances) - 5} 条维护略")

        lines.append(f"来源：{snapshot.page_url or 'https://status.vrchat.com'}")
        return "\n".join(lines)
