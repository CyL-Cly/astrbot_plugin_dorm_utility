# -*- coding: utf-8 -*-
"""AstrBot plugin: query dorm electricity/water via water-monitor API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star


DEFAULT_ALIASES = ("#电费", "#水电", "#宿舍水电")
REFRESH_CMD = "#刷新水电凭证"
LIST_CMD = "#宿舍列表"


def _cfg_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    try:
        if isinstance(config, dict):
            return config.get(key, default)
        if hasattr(config, "get"):
            return config.get(key, default)
    except Exception:
        pass
    try:
        return config[key]
    except Exception:
        return default


def _as_float(value: Any, digits: int = 2) -> str:
    try:
        return f"{round(float(value), digits):.{digits}f}"
    except (TypeError, ValueError):
        return str(value) if value is not None else "-"


def _format_snapshot(raw: Any) -> str:
    if not raw:
        return "-"
    text = str(raw).strip()
    # 2026-10-30T17:00:00+08:00 -> 2026-10-30 17:00
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return text.replace("T", " ")[:16]


def format_dashboard(payload: dict[str, Any]) -> str:
    """Format water-monitor dashboard JSON into a short Chinese reply."""
    room = payload.get("room") or {}
    overview = payload.get("overview") or {}

    build = str(room.get("buildName") or "").strip()
    name = str(room.get("roomName") or "").strip()
    dorm = f"{build} {name}".strip() or f"roomPk={room.get('roomPk', '?')}"

    elec_odd = room.get("latestElecOdd", overview.get("todayElecOdd"))
    elec_use = room.get("latestElecTodayuse", overview.get("todayElecTotal"))
    water_odd = room.get("latestWaterOdd", overview.get("todayWaterOdd"))
    water_use = room.get("latestWaterTodayuse", overview.get("todayWaterTotal"))
    snap = room.get("latestSnapshotAt") or overview.get("latestSnapshotAt")

    return (
        f"宿舍：{dorm}\n"
        f"电费余量：{_as_float(elec_odd)}\n"
        f"今日用电：{_as_float(elec_use)}\n"
        f"水费余量：{_as_float(water_odd)}\n"
        f"今日用水：{_as_float(water_use)}\n"
        f"快照：{_format_snapshot(snap)}"
    )


def format_rooms_list(payload: dict[str, Any]) -> str:
    rooms = payload.get("rooms") or []
    account = str(payload.get("account") or "").strip()
    lines = []
    if account:
        lines.append(f"账号：{account}")
    if not rooms:
        lines.append("未绑定宿舍房间。")
        return "\n".join(lines)
    lines.append(f"已绑定 {len(rooms)} 间：")
    for r in rooms:
        pk = r.get("roomPk")
        build = str(r.get("buildName") or "").strip()
        name = str(r.get("roomName") or "").strip()
        lines.append(f"- roomPk={pk}  {build} {name}".rstrip())
    lines.append("在面板把 room_pk 改成目标房间即可。")
    return "\n".join(lines)


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        logger.info("astrbot_plugin_dorm_utility loaded")

    def _aliases(self) -> set[str]:
        raw = str(_cfg_get(self.config, "command_aliases", "#电费,#水电") or "")
        parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
        aliases = set(parts) if parts else set(DEFAULT_ALIASES)
        # keep convenient defaults even if panel clears some
        aliases.update(DEFAULT_ALIASES)
        return aliases

    def _base_url(self) -> str:
        return str(_cfg_get(self.config, "base_url", "https://tools.rainfog.top/water-monitor") or "").rstrip("/")

    def _token(self) -> str:
        raw = str(_cfg_get(self.config, "access_token", "") or "").strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        return raw

    def _room_pk(self) -> int | None:
        val = _cfg_get(self.config, "room_pk", 1888)
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def _timeout(self) -> float:
        try:
            return max(3.0, float(_cfg_get(self.config, "timeout_sec", 15) or 15))
        except (TypeError, ValueError):
            return 15.0

    def _http_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> tuple[int, Any]:
        url = f"{self._base_url()}{path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "astrbot-plugin-dorm-utility/0.1",
        }
        auth = token if token is not None else self._token()
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout()) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = getattr(resp, "status", 200) or 200
                if not raw:
                    return status, None
                try:
                    return status, json.loads(raw)
                except json.JSONDecodeError:
                    return status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = raw
            return int(e.code), payload
        except urllib.error.URLError as e:
            raise ConnectionError(str(e.reason or e)) from e

    def _save_token(self, token: str) -> bool:
        """Persist access_token into AstrBot plugin config if possible."""
        cfg = self.config
        if cfg is None:
            return False
        try:
            cfg["access_token"] = token
            save = getattr(cfg, "save_config", None)
            if callable(save):
                save()
                return True
            return False
        except Exception as e:
            logger.warning("dorm_utility: failed to save access_token: %s", e)
            return False

    async def _save_token_async(self, token: str) -> bool:
        cfg = self.config
        if cfg is None:
            return False
        try:
            if isinstance(cfg, dict) or hasattr(cfg, "__setitem__"):
                cfg["access_token"] = token
            save_async = getattr(cfg, "save_config_async", None)
            if callable(save_async):
                await save_async()
                return True
            save = getattr(cfg, "save_config", None)
            if callable(save):
                save()
                return True
        except Exception as e:
            logger.warning("dorm_utility: async save access_token failed: %s", e)
        return False

    async def _query_dashboard(self) -> str:
        token = self._token()
        room_pk = self._room_pk()
        if not token or room_pk is None:
            return "请先在 AstrBot 面板填 access_token 和 room_pk。"
        try:
            status, payload = self._http_json("GET", f"/rooms/{room_pk}/dashboard")
        except ConnectionError as e:
            return f"网络错误：{e}"
        except Exception as e:
            return f"查询失败：{e}"
        if status == 401:
            return "凭证失效，请在 AstrBot 面板更新 access_token（或发 #刷新水电凭证）。"
        if status == 404:
            return f"房间不存在或未绑定（room_pk={room_pk}）。可用 #宿舍列表 查看。"
        if status >= 400:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("detail") or payload.get("message") or "")[:120]
            elif isinstance(payload, str):
                detail = payload[:120]
            return f"查询失败（HTTP {status}）{('：' + detail) if detail else ''}。"
        if not isinstance(payload, dict):
            return "查询失败：返回不是 JSON。"
        return format_dashboard(payload)

    async def _list_rooms(self) -> str:
        token = self._token()
        if not token:
            return "请先在 AstrBot 面板填 access_token。"
        try:
            status, payload = self._http_json("GET", "/v1/me/rooms")
        except ConnectionError as e:
            return f"网络错误：{e}"
        except Exception as e:
            return f"查询失败：{e}"
        if status == 401:
            return "凭证失效，请在 AstrBot 面板更新 access_token（或发 #刷新水电凭证）。"
        if status >= 400:
            return f"查询宿舍列表失败（HTTP {status}）。"
        if not isinstance(payload, dict):
            return "查询失败：返回不是 JSON。"
        return format_rooms_list(payload)

    async def _refresh_token(self) -> str:
        account = str(_cfg_get(self.config, "account", "") or "").strip()
        cas = str(_cfg_get(self.config, "cas_cookie", "") or "").strip()
        if not account or not cas:
            return (
                "刷新需要面板里同时填写 account 和 cas_cookie。\n"
                "也可以直接在面板粘贴新的 access_token。"
            )
        try:
            status, payload = self._http_json(
                "POST",
                "/v1/auth/issue",
                body={"account": account, "casCookie": cas},
                token="",  # issue endpoint should not require old bearer
            )
        except ConnectionError as e:
            return f"网络错误：{e}"
        except Exception as e:
            return f"刷新失败：{e}"
        if status == 401 or status == 403:
            return "CAS Cookie 失效或账号无权，请在面板更新 cas_cookie。"
        if status >= 400:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("detail") or payload.get("message") or "")[:120]
            return f"刷新失败（HTTP {status}）{('：' + detail) if detail else ''}。"
        token = ""
        if isinstance(payload, dict):
            token = str(
                payload.get("accessToken")
                or payload.get("access_token")
                or payload.get("token")
                or ""
            ).strip()
            if not token and isinstance(payload.get("data"), dict):
                data = payload["data"]
                token = str(
                    data.get("accessToken")
                    or data.get("access_token")
                    or data.get("token")
                    or ""
                ).strip()
        if not token:
            return "刷新接口未返回 token，请到面板手动粘贴 access_token。"
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        saved = await self._save_token_async(token)
        if saved:
            return "水电凭证已刷新并写入面板配置。"
        return (
            "已拿到新凭证，但无法自动写入配置。\n"
            "请把新 access_token 粘贴到 AstrBot 面板（宿舍水电插件）。"
        )

    @filter.regex(r"^#\S+\s*$")
    async def on_hash_command(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        text = (event.get_message_str() or "").strip()
        if text in self._aliases():
            reply = await self._query_dashboard()
            event.stop_event()
            yield event.plain_result(reply)
            return
        if text == REFRESH_CMD:
            reply = await self._refresh_token()
            event.stop_event()
            yield event.plain_result(reply)
            return
        if text == LIST_CMD:
            reply = await self._list_rooms()
            event.stop_event()
            yield event.plain_result(reply)
            return
        # other #commands (e.g. #过滑块密钥) — do not stop; let client/other plugins handle
