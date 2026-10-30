# Changelog

## 0.2.0

- 定时电费余额提醒（面板可开关）
- `LackBalance`：余额 < `lack_balance_threshold`(默认35) → 每小时查；否则每天 `daily_query_hour` 查一次
- 余额 < `alert_threshold`(默认10) → 向 `notify_targets` 推送；00:00–06:00 推迟到 06:00
- 状态持久化：`plugin_data/.../alert_state.json`

## 0.1.0

- 初版：`#电费` / `#水电` / `#宿舍水电` 查 dashboard
- 面板可配置 `access_token`、`room_pk`、`account`、`cas_cookie`
- `#宿舍列表`、`#刷新水电凭证`
