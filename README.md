# astrbot_plugin_dorm_utility（宿舍水电）

在微信（或其他 AstrBot 平台）发送 `#电费` / `#水电`，查询宿舍水电余量与今日用量；可选**定时余额不足提醒**。

数据来自 [water-monitor](https://tools.rainfog.top/water-monitor)；**凭证、宿舍、提醒目标均可在 AstrBot 插件面板更新**，无需改代码。

## 安装

1. 把本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_dorm_utility/`
2. 重启 / 重载插件
3. 打开 AstrBot 面板 → 插件「宿舍水电」→ 填写：
   - `access_token`：JWT（不要带 `Bearer ` 前缀）
   - `room_pk`：宿舍房间主键（例如 `1888`）
4. 私聊机器人发 `#电费` 试一下

## 面板字段

| 字段 | 说明 |
|------|------|
| `base_url` | API 根地址，默认 `https://tools.rainfog.top/water-monitor` |
| `access_token` | Bearer JWT（面板可更新） |
| `room_pk` | 宿舍房间主键 |
| `account` | 学号（可选，刷新凭证用） |
| `cas_cookie` | CAS Cookie（可选，刷新凭证用） |
| `command_aliases` | 查询别名，逗号分隔，默认 `#电费,#水电` |
| `timeout_sec` | HTTP 超时秒数，默认 15 |
| `alert_enabled` | 是否启用定时查询/提醒（默认关） |
| `alert_threshold` | 低于此电费余量则提醒（默认 10） |
| `lack_balance_threshold` | 低于此值进入每小时查询（默认 35） |
| `daily_query_hour` | 正常时每天几点查（0–23，Asia/Shanghai，默认 9） |
| `notify_targets` | 提醒目标，多人/多群，见下 |
| `notify_platform_id` | 短格式目标的平台实例 ID（须与日志 UMO 前缀一致，如 `wechat_robot_xTVn`） |
| `quiet_start_hour` / `quiet_end_hour` | 静默时段（默认 0–6）；此间提醒推迟到结束时刻 |

**不要把 `access_token` / `cas_cookie` 提交进公开 Git 仓库。**

### 提醒目标怎么填（可多选）

`notify_targets` 支持**逗号或换行**分隔多个目标：

```
wxid_abc123
1234567890@chatroom
wechat_robot:FriendMessage:wxid_abc123
wechat_robot:GroupMessage:1234567890@chatroom
```

- 好友短格式：`wxid_xxx` → `wechat_robot:FriendMessage:wxid_xxx`
- 群短格式：`xxx@chatroom` → `wechat_robot:GroupMessage:xxx@chatroom`
- 也可直接填完整 UMO

填好转发目标后，再把 `alert_enabled` 打开。
> 提示：主动发送用的是平台**实例 id**。看日志里的 UMO（例如 `wechat_robot_xTVn:FriendMessage:...`），把 `notify_platform_id` 设成 `wechat_robot_xTVn`，或直接在 `notify_targets` 填完整 UMO。


## 定时逻辑（Asia/Shanghai）

1. 每次查询（手动 `#电费` 或定时）后：余额 < `lack_balance_threshold` → `LackBalance=1`，否则 `0`
2. `LackBalance != 1`：每天在 `daily_query_hour` 查一次；`LackBalance == 1`：每小时查一次
3. 余额 < `alert_threshold`：向 `notify_targets` 发提醒
4. 若提醒落在静默时段（默认 00:00–06:00）：**推迟到当天 `quiet_end_hour`**，不丢弃
5. 状态文件：`data/plugin_data/astrbot_plugin_dorm_utility/alert_state.json`

## 指令

| 指令 | 作用 |
|------|------|
| `#电费` / `#水电` / `#宿舍水电` | 查询当前 `room_pk` 的水电 |
| `#宿舍列表` | 列出账号已绑定房间（方便改 `room_pk`） |
| `#刷新水电凭证` | 用面板里的 `account` + `cas_cookie` 换新 token 并尽量写回配置 |

不会接管 `#过滑块密钥`（仍由 wechat-robot-client 处理）。

## 示例回复

```
宿舍：学生宿舍16栋海富楼 A616
电费余量：167.64
今日用电：15.31
水费余量：61.4
今日用水：1.10
快照：2026-10-30 22:00
```

## 开发

- 无第三方依赖（stdlib `urllib`）
- 本地格式化冒烟：`PYTHONPATH=/path/to/AstrBot python3 -c "from main import format_dashboard; ..."`
