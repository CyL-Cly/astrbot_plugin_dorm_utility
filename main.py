# -*- coding: utf-8 -*-
"""AstrBot plugin: query dorm electricity/water via water-monitor API + scheduled alerts."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, StarTools


DEFAULT_ALIASES = ("#电费", "#水电", "#宿舍水电")
REFRESH_CMD = "#刷新水电凭证"
LIST_CMD = "#宿舍列表"
TEST_CMDS = ("#水电测试", "#电费测试")
PLUGIN_NAME = "astrbot_plugin_dorm_utility"
TZ = ZoneInfo("Asia/Shanghai")
STATE_FILENAME = "alert_state.json"
PLUGIN_VERSION = "0.2.5"
MAX_HISTORY_DAYS = 30


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


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _room_label(payload: dict[str, Any]) -> str:
    room = payload.get("room") or {}
    build = str(room.get("buildName") or "").strip()
    name = str(room.get("roomName") or "").strip()
    dorm = f"{build} {name}".strip()
    if dorm:
        return dorm
    return f"roomPk={room.get('roomPk', '?')}"


def extract_elec_odd(payload: dict[str, Any]) -> float | None:
    """Prefer latestElecOdd, fall back to todayElecOdd / overview."""
    room = payload.get("room") or {}
    overview = payload.get("overview") or {}
    for key_src in (
        room.get("latestElecOdd"),
        overview.get("todayElecOdd"),
        overview.get("latestElecOdd"),
        room.get("todayElecOdd"),
    ):
        val = _to_float(key_src)
        if val is not None:
            return val
    return None


def format_dashboard(payload: dict[str, Any]) -> str:
    """Format water-monitor dashboard JSON into a short Chinese reply."""
    room = payload.get("room") or {}
    overview = payload.get("overview") or {}

    dorm = _room_label(payload)

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



def parse_relative_day_command(text: str) -> int | None:
    """Parse #昨日水电 / #前天水电 / #-N水电 / #N天前水电 → days_ago (>=1)."""
    t = (text or "").strip()
    if re.fullmatch(r"#\s*昨日水电", t):
        return 1
    if re.fullmatch(r"#\s*前天水电", t):
        return 2
    m = re.fullmatch(r"#\s*-(\d+)\s*水电", t)
    if m:
        n = int(m.group(1))
        return n if n >= 1 else None
    m = re.fullmatch(r"#\s*(\d+)\s*天前\s*水电", t)
    if m:
        n = int(m.group(1))
        return n if n >= 1 else None
    return None


def _day_hint(days_ago: int) -> str:
    if days_ago == 1:
        return "昨日"
    if days_ago == 2:
        return "前天"
    return f"{days_ago}天前"


def format_snapshot_day(
    payload: dict[str, Any],
    *,
    days_ago: int,
    stat_date: str,
) -> str:
    """Format snapshot-day JSON in the same structure as today's dashboard reply."""
    overview = payload.get("overview") or {}
    dorm = _room_label(payload)
    if days_ago == 1:
        elec_label = "昨日用电"
        water_label = "昨日用水"
    else:
        elec_label = "当日用电"
        water_label = "当日用水"
    elec_use = overview.get("roomElec")
    water_use = overview.get("roomWater")
    return (
        f"宿舍：{dorm}\n"
        f"电费余量：-（历史日无）\n"
        f"{elec_label}：{_as_float(elec_use)}\n"
        f"水费余量：-（历史日无）\n"
        f"{water_label}：{_as_float(water_use)}\n"
        f"快照：{stat_date}（{_day_hint(days_ago)}）"
    )


def extract_hourly_elec(payload: dict[str, Any]) -> list[tuple[str, float]]:
    """Extract (hour_label, hourly_kwh_usage) from cumulative hourly.elec series.

    API values are cumulative kWh within the day; convert to per-hour usage:
    usage[0] = value[0], usage[i] = max(0, value[i] - value[i-1]).
    """
    hourly = payload.get("hourly") or {}
    series = hourly.get("elec") or []
    cum: list[tuple[str, float]] = []
    if not isinstance(series, list):
        return []
    for item in series:
        if not isinstance(item, dict):
            continue
        hour = str(item.get("hour", "")).strip()
        val = _to_float(item.get("value"))
        if not hour or val is None:
            continue
        if hour.isdigit():
            hour = f"{int(hour):02d}"
        cum.append((hour, val))
    out: list[tuple[str, float]] = []
    prev: float | None = None
    for hour, val in cum:
        if prev is None:
            usage = max(0.0, val)
        else:
            usage = max(0.0, val - prev)
        out.append((hour, round(usage, 4)))
        prev = val
    return out


def _pick_cjk_font() -> str | None:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for p in candidates:
        if Path(p).is_file():
            return p
    return None


def render_elec_hourly_chart(
    points: list[tuple[str, float]],
    *,
    title: str,
    out_path: Path,
) -> Path | None:
    """Draw electricity-only per-hour usage line chart → PNG. Prefer matplotlib, else PIL."""
    if not points:
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    hours = [h for h, _ in points]
    values = [v for _, v in points]

    # --- matplotlib ---
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager

        font_path = _pick_cjk_font()
        if font_path:
            font_manager.fontManager.addfont(font_path)
            fname = font_manager.FontProperties(fname=font_path).get_name()
            plt.rcParams["font.family"] = fname
        plt.rcParams["axes.unicode_minus"] = False

        fig, ax = plt.subplots(figsize=(8.5, 4.2), dpi=120)
        ax.plot(range(len(values)), values, color="#2563eb", linewidth=2.2, marker="o", markersize=3.5)
        ax.fill_between(range(len(values)), values, color="#2563eb", alpha=0.12)
        ax.set_title(title, fontsize=13)
        ax.set_xlabel("小时")
        ax.set_ylabel("每小时用电（度）")
        ax.set_xticks(range(len(hours)))
        ax.set_xticklabels(hours, fontsize=8)
        ax.grid(True, linestyle="--", alpha=0.35)
        ax.set_ylim(bottom=0)
        fig.tight_layout()
        fig.savefig(out_path, format="png")
        plt.close(fig)
        if out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
    except Exception as e:
        logger.warning("dorm_utility: matplotlib chart failed: %s", e)

    # --- PIL fallback ---
    try:
        from PIL import Image as PILImage, ImageDraw, ImageFont

        W, H = 900, 420
        pad_l, pad_r, pad_t, pad_b = 64, 24, 48, 48
        img = PILImage.new("RGB", (W, H), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        font_path = _pick_cjk_font()
        if font_path:
            try:
                font = ImageFont.truetype(font_path, 18)
                font_sm = ImageFont.truetype(font_path, 12)
            except Exception:
                font_sm = font
        else:
            font_sm = font
        draw.text((pad_l, 12), title, fill=(30, 30, 30), font=font)
        plot_w = W - pad_l - pad_r
        plot_h = H - pad_t - pad_b
        vmax = max(values) if values else 1.0
        if vmax <= 0:
            vmax = 1.0
        # axes
        draw.rectangle([pad_l, pad_t, pad_l + plot_w, pad_t + plot_h], outline=(180, 180, 180))
        n = len(values)
        pts = []
        for i, v in enumerate(values):
            x = pad_l + (0 if n == 1 else int(i * plot_w / (n - 1)))
            y = pad_t + plot_h - int((v / vmax) * plot_h)
            pts.append((x, y))
        if len(pts) >= 2:
            draw.line(pts, fill=(37, 99, 235), width=2)
        for x, y in pts:
            draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(37, 99, 235))
        # x labels every 3h
        for i, h in enumerate(hours):
            if i % 3 != 0 and i != n - 1:
                continue
            x = pts[i][0]
            draw.text((x - 6, pad_t + plot_h + 8), h, fill=(80, 80, 80), font=font_sm)
        draw.text((8, pad_t), f"{vmax:.1f}", fill=(80, 80, 80), font=font_sm)
        draw.text((8, pad_t + plot_h - 12), "0", fill=(80, 80, 80), font=font_sm)
        img.save(out_path, format="PNG")
        if out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
    except Exception as e:
        logger.warning("dorm_utility: PIL chart failed: %s", e)
    return None


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


def format_alert_text(
    payload: dict[str, Any],
    *,
    balance: float,
    threshold: float,
) -> str:
    dorm = _room_label(payload)
    return (
        f"⚠️ 电费余额不足提醒\n"
        f"宿舍：{dorm}\n"
        f"当前电费余量：{_as_float(balance)}\n"
        f"提醒阈值：{_as_float(threshold)}\n"
        f"请及时充值。"
    )


def parse_notify_targets(raw: str, platform_id: str = "wechat_robot") -> list[str]:
    """Parse multi-target string into UMO list."""
    text = str(raw or "").strip()
    if not text:
        return []
    # split by comma / Chinese comma / newline / semicolon
    parts: list[str] = []
    buf = text.replace("，", ",").replace("；", ";").replace(";", ",")
    for line in buf.splitlines():
        for piece in line.split(","):
            p = piece.strip()
            if p:
                parts.append(p)
    platform = (platform_id or "wechat_robot").strip() or "wechat_robot"
    umos: list[str] = []
    seen: set[str] = set()
    for p in parts:
        umo = _normalize_target(p, platform)
        if umo and umo not in seen:
            seen.add(umo)
            umos.append(umo)
    return umos


def _normalize_target(raw: str, platform: str) -> str | None:
    s = raw.strip()
    if not s:
        return None
    # Full UMO: platform:MessageType:session
    if s.count(":") >= 2:
        try:
            plat, mtype, sid = s.split(":", 2)
            if plat and mtype and sid:
                return f"{plat}:{mtype}:{sid}"
        except ValueError:
            pass
    # Short: group chatroom
    if "@chatroom" in s.lower():
        return f"{platform}:GroupMessage:{s}"
    # Short: friend wxid / qq number etc.
    return f"{platform}:FriendMessage:{s}"


def _clamp_hour(value: Any, default: int) -> int:
    try:
        h = int(value)
    except (TypeError, ValueError):
        return default
    if h < 0 or h > 23:
        return default
    return h


def _now_shanghai() -> datetime:
    return datetime.now(TZ)


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self._alert_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._test_send_lock = asyncio.Lock()
        self._register_web_apis()
        logger.info("astrbot_plugin_dorm_utility loaded")

    # ---- config helpers ----

    def _aliases(self) -> set[str]:
        raw = str(_cfg_get(self.config, "command_aliases", "#电费,#水电") or "")
        parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
        aliases = set(parts) if parts else set(DEFAULT_ALIASES)
        aliases.update(DEFAULT_ALIASES)
        return aliases

    def _base_url(self) -> str:
        return str(
            _cfg_get(self.config, "base_url", "https://tools.rainfog.top/water-monitor")
            or ""
        ).rstrip("/")

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

    def _alert_enabled(self) -> bool:
        return bool(_cfg_get(self.config, "alert_enabled", False))

    def _alert_threshold(self) -> float:
        try:
            return float(_cfg_get(self.config, "alert_threshold", 10) or 10)
        except (TypeError, ValueError):
            return 10.0

    def _lack_threshold(self) -> float:
        try:
            return float(_cfg_get(self.config, "lack_balance_threshold", 35) or 35)
        except (TypeError, ValueError):
            return 35.0

    def _daily_hour(self) -> int:
        return _clamp_hour(_cfg_get(self.config, "daily_query_hour", 9), 9)

    def _quiet_hours(self) -> tuple[int, int]:
        start = _clamp_hour(_cfg_get(self.config, "quiet_start_hour", 0), 0)
        end = _clamp_hour(_cfg_get(self.config, "quiet_end_hour", 6), 6)
        return start, end

    def _notify_umos(self) -> list[str]:
        raw = str(_cfg_get(self.config, "notify_targets", "") or "")
        platform = str(_cfg_get(self.config, "notify_platform_id", "wechat_robot") or "wechat_robot")
        return parse_notify_targets(raw, platform)

    def _send_test_now_flag(self) -> bool:
        return bool(_cfg_get(self.config, "send_test_now", False))

    # ---- HTTP ----

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
            "User-Agent": f"astrbot-plugin-dorm-utility/{PLUGIN_VERSION}",
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

    # ---- state persistence ----

    def _data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            fallback = Path(__file__).resolve().parent / "data"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _state_path(self) -> Path:
        return self._data_dir() / STATE_FILENAME

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        default = {
            "LackBalance": 0,
            "last_query_at": None,
            "last_query_day": None,
            "last_hourly_slot": None,
            "pending_deferred_alert": None,
            "last_alert_at": None,
        }
        if not path.is_file():
            return dict(default)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return dict(default)
            out = dict(default)
            out.update(raw)
            return out
        except Exception as e:
            logger.warning("dorm_utility: load state failed: %s", e)
            return dict(default)

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:
            logger.warning("dorm_utility: save state failed: %s", e)

    # ---- dashboard / snapshot-day fetch ----

    def _http_error_message(
        self,
        status: int,
        payload: Any,
        *,
        room_pk: int | None,
        what: str = "查询",
    ) -> str | None:
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
            return f"{what}失败（HTTP {status}）{('：' + detail) if detail else ''}。"
        if not isinstance(payload, dict):
            return f"{what}失败：返回不是 JSON。"
        return None

    def _fetch_dashboard_payload(self) -> tuple[str | None, dict[str, Any] | None]:
        """Return (error_message, payload). One of them is set."""
        token = self._token()
        room_pk = self._room_pk()
        if not token or room_pk is None:
            return "请先在 AstrBot 面板填 access_token 和 room_pk。", None
        try:
            status, payload = self._http_json("GET", f"/rooms/{room_pk}/dashboard")
        except ConnectionError as e:
            return f"网络错误：{e}", None
        except Exception as e:
            return f"查询失败：{e}", None
        err = self._http_error_message(status, payload, room_pk=room_pk)
        if err:
            return err, None
        return None, payload  # type: ignore[return-value]

    def _fetch_snapshot_day_payload(
        self, stat_date: str
    ) -> tuple[str | None, dict[str, Any] | None]:
        """GET /rooms/{pk}/snapshot-day?stat_date=YYYY-MM-DD"""
        token = self._token()
        room_pk = self._room_pk()
        if not token or room_pk is None:
            return "请先在 AstrBot 面板填 access_token 和 room_pk。", None
        qs = urllib.parse.urlencode({"stat_date": stat_date})
        path = f"/rooms/{room_pk}/snapshot-day?{qs}"
        try:
            status, payload = self._http_json("GET", path)
        except ConnectionError as e:
            return f"网络错误：{e}", None
        except Exception as e:
            return f"查询失败：{e}", None
        err = self._http_error_message(status, payload, room_pk=room_pk, what="历史查询")
        if err:
            return err, None
        return None, payload  # type: ignore[return-value]

    def _chart_dir(self) -> Path:
        d = self._data_dir() / "charts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cleanup_old_charts(self, keep: int = 20) -> None:
        try:
            files = sorted(
                self._chart_dir().glob("elec_*.png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in files[keep:]:
                try:
                    stale.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _make_elec_chart(
        self,
        payload: dict[str, Any],
        *,
        title: str,
        tag: str,
    ) -> Path | None:
        points = extract_hourly_elec(payload)
        if not points:
            return None
        stamp = _now_shanghai().strftime("%Y%m%d_%H%M%S")
        out = self._chart_dir() / f"elec_{tag}_{stamp}.png"
        path = render_elec_hourly_chart(points, title=title, out_path=out)
        self._cleanup_old_charts()
        return path

    def _apply_lack_balance(self, state: dict[str, Any], balance: float | None) -> None:
        if balance is None:
            return
        lack_th = self._lack_threshold()
        state["LackBalance"] = 1 if balance < lack_th else 0

    async def _query_dashboard(self) -> tuple[str, Path | None]:
        """Today's dashboard text + optional electricity chart path."""
        err, payload = self._fetch_dashboard_payload()
        if err:
            return err, None
        assert payload is not None
        balance = extract_elec_odd(payload)
        state = self._load_state()
        self._apply_lack_balance(state, balance)
        state["last_query_at"] = _now_shanghai().isoformat()
        self._save_state(state)
        text = format_dashboard(payload)
        hourly = payload.get("hourly") or {}
        day = str(hourly.get("date") or _now_shanghai().strftime("%Y-%m-%d"))
        chart = self._make_elec_chart(
            payload,
            title=f"电费用量折线（{day}）",
            tag=f"today_{day}",
        )
        return text, chart

    async def _query_history(self, days_ago: int) -> tuple[str, Path | None]:
        """Historical snapshot-day text + electricity chart."""
        if days_ago < 1:
            return "历史查询天数须 ≥ 1。", None
        if days_ago > MAX_HISTORY_DAYS:
            return f"最多可查最近 {MAX_HISTORY_DAYS} 天。", None
        today = _now_shanghai().date()
        target: date = today - timedelta(days=days_ago)
        stat_date = target.strftime("%Y-%m-%d")
        err, payload = self._fetch_snapshot_day_payload(stat_date)
        if err:
            return err, None
        assert payload is not None
        # Prefer API-reported date when present
        api_date = str(
            (payload.get("statDate") or (payload.get("hourly") or {}).get("date") or stat_date)
        )
        text = format_snapshot_day(payload, days_ago=days_ago, stat_date=api_date)
        chart = self._make_elec_chart(
            payload,
            title=f"电费用量折线（{api_date} · {_day_hint(days_ago)}）",
            tag=f"hist_{api_date}",
        )
        return text, chart

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

    # ---- test push ----

    def _build_test_message(self) -> str:
        """Build Chinese test text; include live elec balance when possible."""
        err, payload = self._fetch_dashboard_payload()
        if payload is not None:
            balance = extract_elec_odd(payload)
            xxx = _as_float(balance) if balance is not None else "-"
            return f"余额提醒消息测试，当前电费余量:{xxx}"
        detail = err or "凭证或宿舍未配置"
        return f"余额提醒消息测试，当前电费余量:未知（{detail}）"

    async def _clear_send_test_flag(self) -> bool:
        """Reset send_test_now to false in plugin config (file save only)."""
        cfg = self.config
        if cfg is None:
            return False
        try:
            if isinstance(cfg, dict) or hasattr(cfg, "__setitem__"):
                cfg["send_test_now"] = False
            save_async = getattr(cfg, "save_config_async", None)
            if callable(save_async):
                await save_async()
                return True
            save = getattr(cfg, "save_config", None)
            if callable(save):
                save()
                return True
        except Exception as e:
            logger.warning("dorm_utility: clear send_test_now failed: %s", e)
        return False

    async def _send_test_alert(self, *, source: str = "manual") -> dict[str, Any]:
        """One-shot test to notify_targets. Does not update alert_state."""
        async with self._test_send_lock:
            umos = self._notify_umos()
            if not umos:
                msg = "notify_targets 为空，请先在面板填写提醒目标。"
                logger.warning("dorm_utility: test skipped — %s", msg)
                return {"ok": False, "sent": 0, "targets": 0, "message": msg, "source": source}
            text = self._build_test_message()
            sent = await self._send_alert_text(text)
            ok = sent > 0
            result = {
                "ok": ok,
                "sent": sent,
                "targets": len(umos),
                "message": (
                    f"已向 {sent}/{len(umos)} 个目标发送测试消息。"
                    if ok
                    else f"发送失败（0/{len(umos)}）。请检查 notify_platform_id / 适配器是否在线。"
                ),
                "preview": text,
                "source": source,
            }
            logger.info(
                "dorm_utility: test push source=%s sent=%s/%s",
                source,
                sent,
                len(umos),
            )
            return result

    def _register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning("dorm_utility: register_web_api unavailable")
            return
        try:
            register(
                f"/{PLUGIN_NAME}/send-test",
                self._api_send_test,
                ["POST"],
                "Send dorm utility test alert to notify_targets",
            )
        except Exception as e:
            logger.warning("dorm_utility: register web api failed: %s", e)

    async def _api_send_test(self):
        try:
            from astrbot.api.web import error_response, json_response
        except Exception:
            # very old AstrBot — return plain dict (may still work via bridge)
            result = await self._send_test_alert(source="plugin_view")
            return result
        try:
            result = await self._send_test_alert(source="plugin_view")
            if not result.get("ok"):
                # still 200 with ok=false so UI can show message
                return json_response(result)
            return json_response(result)
        except Exception as e:
            logger.warning("dorm_utility: api send-test error: %s", e)
            return error_response(str(e))

    # ---- quiet hours / notify ----

    def _in_quiet_hours(self, now: datetime | None = None) -> bool:
        now = now or _now_shanghai()
        start, end = self._quiet_hours()
        hour = now.hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        # wraps midnight e.g. 22-6
        return hour >= start or hour < end

    def _quiet_end_datetime(self, now: datetime | None = None) -> datetime:
        now = now or _now_shanghai()
        _, end = self._quiet_hours()
        candidate = now.replace(hour=end, minute=0, second=0, microsecond=0)
        if candidate <= now:
            # already past today's quiet end → next day (wrap case)
            if self._in_quiet_hours(now):
                # still in quiet; if end already passed relative to wrap, push to next calendar day
                candidate = candidate + timedelta(days=1)
        # For standard 0-6: if now is 1:00, end is today 06:00
        if self._in_quiet_hours(now) and candidate <= now:
            candidate = candidate + timedelta(days=1)
        # Recompute cleanly for 0-6 case
        start, end = self._quiet_hours()
        if start < end:
            # quiet within same day
            end_dt = now.replace(hour=end, minute=0, second=0, microsecond=0)
            if now < end_dt:
                return end_dt
            return end_dt + timedelta(days=1)
        # wrap midnight: quiet from start..24 and 0..end
        end_dt = now.replace(hour=end, minute=0, second=0, microsecond=0)
        if now.hour >= start:
            # after start → end is tomorrow
            return end_dt + timedelta(days=1)
        return end_dt

    async def _send_alert_text(self, text: str) -> int:
        umos = self._notify_umos()
        if not umos:
            logger.warning("dorm_utility: alert skipped — notify_targets empty")
            return 0
        chain = MessageChain().message(text)
        ok = 0
        for umo in umos:
            try:
                sent = await self.context.send_message(umo, chain)
                if sent:
                    ok += 1
                    logger.info("dorm_utility: alert sent to %s", umo)
                else:
                    logger.warning("dorm_utility: no platform matched for %s", umo)
            except Exception as e:
                logger.warning("dorm_utility: send to %s failed: %s", umo, e)
        return ok

    async def _maybe_alert(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
        balance: float,
    ) -> None:
        threshold = self._alert_threshold()
        if balance >= threshold:
            # clear deferred if balance recovered
            if state.get("pending_deferred_alert"):
                state["pending_deferred_alert"] = None
            return
        text = format_alert_text(payload, balance=balance, threshold=threshold)
        now = _now_shanghai()
        if self._in_quiet_hours(now):
            fire_at = self._quiet_end_datetime(now)
            # Do not store final alert text — flush refreshes live balance first.
            state["pending_deferred_alert"] = {
                "fire_at": fire_at.isoformat(),
                "created_at": now.isoformat(),
                "balance_at_defer": balance,
            }
            logger.info(
                "dorm_utility: alert deferred to %s (quiet hours); balance_at_defer=%s",
                fire_at.isoformat(),
                balance,
            )
            return
        # clear any stale deferred and send now
        state["pending_deferred_alert"] = None
        n = await self._send_alert_text(text)
        if n:
            state["last_alert_at"] = now.isoformat()

    async def _flush_deferred(self, state: dict[str, Any]) -> None:
        pending = state.get("pending_deferred_alert")
        if not isinstance(pending, dict):
            return
        fire_raw = pending.get("fire_at")
        if not fire_raw:
            state["pending_deferred_alert"] = None
            return
        try:
            fire_at = datetime.fromisoformat(str(fire_raw))
            if fire_at.tzinfo is None:
                fire_at = fire_at.replace(tzinfo=TZ)
        except Exception:
            state["pending_deferred_alert"] = None
            return
        now = _now_shanghai()
        if now < fire_at:
            return
        if self._in_quiet_hours(now):
            # still quiet somehow — wait
            return

        # Refresh live balance before sending (never use stale deferred text).
        err, payload = self._fetch_dashboard_payload()
        if err or payload is None:
            logger.warning(
                "dorm_utility: deferred alert refresh failed (%s); keep pending for retry",
                err or "empty payload",
            )
            return

        balance = extract_elec_odd(payload)
        if balance is None:
            logger.warning(
                "dorm_utility: deferred alert: cannot parse balance; keep pending for retry"
            )
            return

        self._apply_lack_balance(state, balance)
        threshold = self._alert_threshold()
        if balance >= threshold:
            state["pending_deferred_alert"] = None
            logger.info(
                "dorm_utility: deferred alert cancelled — balance recovered to %s (>= %s)",
                balance,
                threshold,
            )
            return

        text = format_alert_text(payload, balance=balance, threshold=threshold)
        n = await self._send_alert_text(text)
        if n:
            state["pending_deferred_alert"] = None
            state["last_alert_at"] = now.isoformat()
            logger.info(
                "dorm_utility: deferred alert flushed with current balance=%s",
                balance,
            )
        else:
            logger.warning(
                "dorm_utility: deferred alert send failed; keep pending for retry"
            )

    def _should_run_scheduled_query(self, state: dict[str, Any], now: datetime) -> bool:
        lack = int(state.get("LackBalance") or 0)
        last_at_raw = state.get("last_query_at")
        last_at: datetime | None = None
        if last_at_raw:
            try:
                last_at = datetime.fromisoformat(str(last_at_raw))
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=TZ)
            except Exception:
                last_at = None

        if lack == 1:
            # every hour: once per clock hour
            slot = now.strftime("%Y-%m-%d-%H")
            if state.get("last_hourly_slot") == slot:
                return False
            # if we just queried within ~50 min, skip (avoid double after mode switch)
            if last_at and (now - last_at).total_seconds() < 50 * 60:
                return False
            return True

        # daily once at daily_query_hour
        day = now.strftime("%Y-%m-%d")
        if state.get("last_query_day") == day:
            return False
        if now.hour < self._daily_hour():
            return False
        return True

    async def _run_scheduled_query(self, state: dict[str, Any]) -> None:
        err, payload = self._fetch_dashboard_payload()
        now = _now_shanghai()
        if err:
            logger.warning("dorm_utility: scheduled query failed: %s", err)
            return
        assert payload is not None
        balance = extract_elec_odd(payload)
        self._apply_lack_balance(state, balance)
        state["last_query_at"] = now.isoformat()
        state["last_query_day"] = now.strftime("%Y-%m-%d")
        state["last_hourly_slot"] = now.strftime("%Y-%m-%d-%H")
        logger.info(
            "dorm_utility: scheduled query ok balance=%s LackBalance=%s",
            balance,
            state.get("LackBalance"),
        )
        if balance is not None:
            await self._maybe_alert(state, payload, balance)

    async def _alert_loop(self) -> None:
        logger.info("dorm_utility: alert loop started")
        # small delay so platforms finish boot
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=15)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                async with self._tick_lock:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("dorm_utility: alert tick error: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                continue
        logger.info("dorm_utility: alert loop stopped")

    async def _tick(self) -> None:
        state = self._load_state()
        # always try flush deferred when enabled (or even if just disabled after defer)
        if self._alert_enabled() or state.get("pending_deferred_alert"):
            await self._flush_deferred(state)
        if self._alert_enabled():
            now = _now_shanghai()
            if self._should_run_scheduled_query(state, now):
                await self._run_scheduled_query(state)
        self._save_state(state)

    # ---- lifecycle ----

    async def initialize(self) -> None:
        self._stop.clear()
        if self._alert_task and not self._alert_task.done():
            # still honor one-shot panel trigger on hot-reload path
            await self._maybe_fire_panel_test()
            return
        self._alert_task = asyncio.create_task(
            self._alert_loop(),
            name="dorm_utility_alert_loop",
        )
        await self._maybe_fire_panel_test()
        logger.info(
            "dorm_utility: initialize done (alert_enabled=%s)",
            self._alert_enabled(),
        )

    async def _maybe_fire_panel_test(self) -> None:
        if not self._send_test_now_flag():
            return
        logger.info("dorm_utility: send_test_now=true — firing one-shot test")
        try:
            result = await self._send_test_alert(source="panel_config")
            logger.info("dorm_utility: panel test result: %s", result.get("message"))
        finally:
            cleared = await self._clear_send_test_flag()
            logger.info("dorm_utility: send_test_now cleared=%s", cleared)

    async def terminate(self) -> None:
        self._stop.set()
        task = self._alert_task
        self._alert_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        logger.info("dorm_utility: terminated")

    # ---- commands ----

    def _yield_text_and_optional_chart(
        self,
        event: AstrMessageEvent,
        reply: str,
        chart: Path | None,
    ) -> MessageEventResult:
        if chart is not None and Path(chart).is_file():
            return event.make_result().message(reply).file_image(str(chart))
        return event.plain_result(reply)

    @filter.regex(r"^#\s*.+\s*$")
    async def on_hash_command(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        text = (event.get_message_str() or "").strip()
        # normalize "# 电费" → keep original for history parser; aliases are exact
        compact = re.sub(r"^#\s+", "#", text)

        days_ago = parse_relative_day_command(text)
        if days_ago is not None:
            reply, chart = await self._query_history(days_ago)
            event.stop_event()
            yield self._yield_text_and_optional_chart(event, reply, chart)
            return

        if text in self._aliases() or compact in self._aliases():
            reply, chart = await self._query_dashboard()
            event.stop_event()
            yield self._yield_text_and_optional_chart(event, reply, chart)
            return
        if text == REFRESH_CMD or compact == REFRESH_CMD:
            reply = await self._refresh_token()
            event.stop_event()
            yield event.plain_result(reply)
            return
        if text == LIST_CMD or compact == LIST_CMD:
            reply = await self._list_rooms()
            event.stop_event()
            yield event.plain_result(reply)
            return
        if text in TEST_CMDS or compact in TEST_CMDS:
            result = await self._send_test_alert(source="chat_command")
            event.stop_event()
            yield event.plain_result(str(result.get("message") or "测试推送已执行。"))
            return
        # other #commands (e.g. #过滑块密钥) — do not stop; let client/other plugins handle
