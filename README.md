# astrbot_plugin_dorm_utility（宿舍水电）

在微信（或其他 AstrBot 平台）发送 `#电费` / `#水电`，查询宿舍水电余量与今日用量。

数据来自 [water-monitor](https://tools.rainfog.top/water-monitor)；**凭证与宿舍房间可在 AstrBot 插件面板随时更新**，无需改代码。

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

**不要把 `access_token` / `cas_cookie` 提交进公开 Git 仓库。**

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
- 本地格式化冒烟：`python3 -c "from main import format_dashboard; ..."`
