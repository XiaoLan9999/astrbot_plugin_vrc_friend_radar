"""VRChat 官方状态页查询与播报配置命令。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from ..core.official_status import describe_official_status_exception

if TYPE_CHECKING:
    from ..main import VRCFriendRadarPlugin


class OfficialStatusCommandsMixin:
    """官方状态页命令 Mixin，self 即为插件实例。"""

    async def _build_official_status_reply(self: 'VRCFriendRadarPlugin') -> str:
        try:
            snapshot = await self.official_status.fetch_once(timeout_seconds=3.5)
            return self.official_status.format_snapshot(snapshot)
        except Exception as exc:
            cached = self.official_status.get_last_snapshot()
            if cached:
                detail = describe_official_status_exception(exc)
                return (
                    f"⚠️ 实时查询 VRChat 官方状态失败：{detail}\n"
                    "以下为最近一次缓存结果：\n"
                    f"{self.official_status.format_snapshot(cached)}"
                )
            raise

    def _write_official_status_config_value(self: 'VRCFriendRadarPlugin', key: str, value: Any) -> None:
        cfg = self.cfg.raw_config
        try:
            if isinstance(cfg, dict):
                cfg[key] = value
            elif hasattr(cfg, "__setitem__"):
                try:
                    cfg[key] = value
                except Exception:
                    setattr(cfg, key, value)
            else:
                setattr(cfg, key, value)
            if hasattr(cfg, "save_config"):
                cfg.save_config()
        except Exception as exc:
            logger.warning(f"[vrc_friend_radar] 写回 {key} 失败: {exc}")

    @filter.command("vrc官方状态")
    async def official_status_query(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        try:
            text = await self._build_official_status_reply()
        except Exception as exc:
            detail = describe_official_status_exception(exc)
            logger.warning(f"[vrc_friend_radar] 查询官方状态失败: {detail}")
            yield event.plain_result(f"查询 VRChat 官方状态失败：{detail}")
            return
        yield event.plain_result(text)

    @filter.command("vrc服务器状态")
    async def official_server_status_query(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        try:
            text = await self._build_official_status_reply()
        except Exception as exc:
            detail = describe_official_status_exception(exc)
            logger.warning(f"[vrc_friend_radar] 查询官方服务器状态失败: {detail}")
            yield event.plain_result(f"查询 VRChat 官方服务器状态失败：{detail}")
            return
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc绑定官方状态群")
    async def bind_official_status_group(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令需要在群聊中使用。")
            return
        groups = self.official_status.add_notify_group(group_id)
        yield event.plain_result(f"已绑定官方状态播报群 {group_id}，当前播报群数量：{len(groups)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc解绑官方状态群")
    async def unbind_official_status_group(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("该命令需要在群聊中使用。")
            return
        groups = self.official_status.remove_notify_group(group_id)
        yield event.plain_result(f"已解绑官方状态播报群 {group_id}，当前播报群数量：{len(groups)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc官方状态群")
    async def show_official_status_groups(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        groups = self.official_status.get_effective_notify_groups()
        lines = [self.official_status.get_runtime_summary()]
        lines.append("")
        if groups:
            lines.append("官方状态播报群：")
            lines.extend(f"- {group_id}" for group_id in groups)
        else:
            lines.append("当前没有配置官方状态播报群。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("vrc官方状态监控")
    async def toggle_official_status_monitor(self: 'VRCFriendRadarPlugin', event: AiocqhttpMessageEvent):
        raw = event.message_str.replace("vrc官方状态监控", "", 1).strip()
        if raw in {"开启", "on", "enable"}:
            self.cfg.enable_official_status_monitor = True
            self._write_official_status_config_value("enable_official_status_monitor", True)
            self.official_status.start()
            yield event.plain_result(
                f"已开启 VRChat 官方状态监控；轮询间隔 {self.cfg.official_status_poll_interval_seconds}s，"
                "出现故障/维护/恢复时会推送到官方状态播报群。"
            )
            return

        if raw in {"关闭", "off", "disable"}:
            self.cfg.enable_official_status_monitor = False
            self._write_official_status_config_value("enable_official_status_monitor", False)
            await self.official_status.stop()
            yield event.plain_result("已关闭 VRChat 官方状态监控；手动 /vrc官方状态 查询不受影响。")
            return

        status = "开启" if self.cfg.enable_official_status_monitor else "关闭"
        yield event.plain_result(
            f"当前 VRChat 官方状态监控：{status}\n"
            f"轮询间隔：{self.cfg.official_status_poll_interval_seconds}s\n"
            "用法：/vrc官方状态监控 开启|关闭"
        )
