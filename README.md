# astrbot_plugin_qq_offline_alarm（QQ 掉线哨兵）

轮询 OneBot(NapCat) 的 `get_status` 接口判断 QQ 登录状态：**QQ 掉线持续超过阈值后，通过 QQ 邮箱 SMTP 发送掉线告警邮件；恢复在线后可选发送恢复通知。**

> 创作者：**小星萤**
> 版本：`1.1.0`
> 支持平台：AstrBot `aiocqhttp` + NapCat

## 功能

- 定期调用 OneBot `get_status`，读取 `online` 字段判断 QQ 是否在线（NapCat 源码中该字段即当前登录状态）。
- 连续离线超过阈值（默认 90 秒）才发送掉线告警邮件，避免短暂网络波动误报。
- 同一次掉线只发送一封告警邮件，状态持久化到 AstrBot 数据目录，重启不会重复轰炸。
- QQ 恢复在线后，可选发送恢复通知邮件（默认开启）。
- 支持多 QQ 账号：留空 `platform_ids` 时自动监控全部 `aiocqhttp` 平台。
- 提供状态查询与测试邮件指令。
- 邮件通过 Python 标准库 `smtplib` 发送，**无任何第三方依赖**。

## 工作原理

```text
NapCat(OneBot v11)
   │ get_status → { online: bool, ... }
   ▼
AstrBot aiocqhttp 客户端
   │ 每 check_interval_seconds 轮询一次
   ▼
QQ 掉线哨兵插件
   │ 连续离线 ≥ offline_threshold_seconds
   ▼
SMTP(QQ 邮箱) → 收件邮箱告警邮件
```

`get_status` 是 OneBot v11 标准接口，NapCat 实现中 `online` 直接来自 `selfInfo.online`，与 QQ 实际登录状态一致；`get_login_info` 用于获取 QQ 号与昵称作为邮件标识。

## 安装

将本目录作为 AstrBot 插件安装，然后在 WebUI 中打开插件配置。插件没有额外的第三方 Python 依赖。

需要满足：

1. AstrBot 使用 `aiocqhttp` 适配器连接 QQ。
2. 协议端为 NapCat（或其他支持 `get_status` 的 OneBot v11 实现）。
3. 已在 QQ 邮箱中开启 SMTP 服务并取得**授权码**（不是邮箱登录密码）。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用监控与告警 |
| `platform_ids` | 空 | 多 QQ 账号时指定监控的平台 ID；留空自动监控全部 |
| `check_interval_seconds` | `30` | 状态检查间隔（秒） |
| `offline_threshold_seconds` | `90` | 连续离线超过该时长才发告警邮件（建议为间隔的 2~3 倍） |
| `smtp_host` | `smtp.qq.com` | SMTP 服务器地址 |
| `smtp_port` | `465` | SMTP 端口（465=SSL，587=STARTTLS） |
| `smtp_ssl` | `true` | true 用 SSL 直连；false 用 STARTTLS |
| `smtp_user` | 空 | 发件 QQ 邮箱地址，如 `123456@qq.com` |
| `smtp_auth_code` | 空 | 发件邮箱 SMTP **授权码**（敏感信息，不会写入日志） |
| `smtp_from_name` | 空 | 发件人显示名称，留空使用发件邮箱 |
| `notify_emails` | 空 | 接收告警邮件的邮箱，多个用逗号/空格/换行分隔；**留空不发送** |
| `notify_on_recover` | `true` | 恢复在线时是否发送恢复通知 |
| `mail_subject_offline` | 空 | 掉线邮件主题模板，留空用默认；支持 `{qq} {nickname} {offline_at} {now} {duration}` |
| `mail_body_offline` | 空 | 掉线邮件正文模板（多行），留空用默认；变量同上 |
| `mail_subject_recover` | 空 | 恢复邮件主题模板，留空用默认；支持 `{qq} {nickname} {recover_at} {duration}` |
| `mail_body_recover` | 空 | 恢复邮件正文模板（多行），留空用默认；变量同上 |
| `admin_list` | 空 | 允许执行 `/掉线哨兵测试` 的管理员 QQ 号 |

修改配置后，在 AstrBot WebUI 中重载插件即可生效。

## 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/掉线哨兵 状态` | 所有人 | 查看各 QQ 在线状态、检测参数与 SMTP 配置摘要 |
| `/掉线哨兵 测试` | 管理员（`admin_list`） | 发送一封测试邮件，验证 SMTP 配置 |
| `/掉线哨兵 帮助` | 所有人 | 显示帮助 |

指令别名：`/offline 状态`、`/offline 测试`、`/offline 帮助`。

## 邮件内容

**掉线告警**（主题 `[QQ掉线提醒] QQ <QQ号> 已掉线`）：

```text
QQ 账号检测到掉线！

QQ 号：3669621853
昵称：xxx
检测到离线时间：2026-08-20 10:30:00
当前时间：2026-08-20 10:31:32
持续离线：1 分 32 秒

请尽快检查 NapCat / QQ 登录状态。
```

**恢复通知**（主题 `[QQ恢复通知] QQ <QQ号> 已恢复在线`）包含恢复时间与离线时长。

## 获取 QQ 邮箱 SMTP 授权码（图文步骤）

QQ 邮箱的 SMTP 发信需要**授权码**（16 位），**不是** QQ 密码。获取步骤如下：

1. **电脑浏览器**打开 <https://mail.qq.com>，扫码或用密码登录你计划作为发件人的 QQ 邮箱。
2. 点击顶部 **「设置」→「账号」**（新版界面在「设置」→「账户安全」里也能找到），向下找到 **「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」** 区块。
3. 找到 **「IMAP/SMTP 服务」**（或只开启「SMTP 服务」；建议同时开启 IMAP/SMTP，方便以后自查收件），点击 **「开启」**。
4. 按页面提示，用绑定该 QQ 的**手机号发送指定内容的短信**到指定号码（普通短信，免费），点击 **「我已发送」**。
5. 验证通过后，页面会弹出一串 **16 位授权码**（大小写字母和数字组成），**复制并保存好**（只在弹出时完整显示一次）。
6. 把授权码填入插件配置的 `smtp_auth_code`，把邮箱地址填入 `smtp_user`。

注意：

- **授权码 ≠ 登录密码**，不要用 QQ 密码填写。
- 一个 QQ 邮箱可以多次生成授权码，**新授权码会令旧授权码立即失效**；忘了就重新生成一次。
- 建议使用**正常在用的 QQ 号**对应的邮箱。已注销的 QQ 号邮箱即使能登录 SMTP，发出的邮件也可能被 QQ 服务端拦截丢弃（本插件 v1.0.1 起已修复发件人头格式，但发件账号本身要有效）。
- 服务器所在地区网络正常访问 `smtp.qq.com`（465/SSL）即可，无需代理。

## 自定义邮件内容

掉线告警与恢复通知的主题、正文都支持模板自定义（v1.1.0 起）。配置留空时使用内置默认模板。

**掉线邮件可用变量：**

| 变量 | 含义 |
| --- | --- |
| `{qq}` | QQ 号 |
| `{nickname}` | QQ 昵称 |
| `{offline_at}` | 检测到离线的时间（北京时间） |
| `{now}` | 发送邮件的时间（北京时间） |
| `{duration}` | 已持续离线时长（如 `1 分 32 秒`） |

**恢复邮件可用变量：**

| 变量 | 含义 |
| --- | --- |
| `{qq}` | QQ 号 |
| `{nickname}` | QQ 昵称 |
| `{recover_at}` | 恢复在线的时间（北京时间） |
| `{duration}` | 本次离线时长 |

示例（掉线邮件正文）：

```text
⚠️ 机器人掉线警告 ⚠️
QQ {qq}（{nickname}）已于 {offline_at} 掉线，
已持续 {duration}。
请尽快处理！当前时间：{now}
```

填到 `mail_body_offline`（支持多行）；主题填到 `mail_subject_offline`，例如 `[紧急] QQ {qq} 已掉线 {duration}`。修改后在 WebUI 重载插件生效。

## 数据位置

运行状态保存在：

```text
data/plugin_data/astrbot_plugin_qq_offline_alarm/state.json
```

状态文件中保存各账号最后在线状态、掉线起始时间与告警标记，不含聊天内容与邮箱授权码。

## 常见问题

### 收不到掉线邮件

依次检查：

1. `notify_emails` 是否填写了正确的收件邮箱（留空不会发送）。
2. `smtp_user` / `smtp_auth_code` 是否正确；授权码需在 QQ 邮箱 → 设置 → 账户 中开启 SMTP 后生成。
3. `smtp_port`/`smtp_ssl` 组合是否与 QQ 邮箱要求一致（465+SSL 或 587+STARTTLS）。
4. 先在群内执行 `/掉线哨兵 测试` 验证 SMTP 是否可用。

### 提示「未找到可用客户端」

检查 aiocqhttp 平台是否已连接；多 QQ 账号时确认 `platform_ids` 填写正确。

### 掉线后一直没收到邮件

确认 `offline_threshold_seconds` 是否过大，以及 NapCat 与 AstrBot 的连接是否仍然正常（NapCat 进程若完全停止，本插件也无法执行检查；QQ 账号掉线但 NapCat 进程存活时检测有效）。

### 邮件里为什么没有日志中的时间

邮件时间统一按北京时间（Asia/Shanghai）展示，与服务器/容器时区无关。

## 项目结构

```text
astrbot_plugin_qq_offline_alarm/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── logo.png
└── README.md
```

## 作者

**小星萤**

## 更新日志

### v1.1.0（2026-10-10）

- **新增：邮件内容自定义模板**。掉线告警与恢复通知的主题、正文均可通过配置 `mail_subject_offline` / `mail_body_offline` / `mail_subject_recover` / `mail_body_recover` 自定义，支持 `{qq} {nickname} {offline_at} {now} {duration} {recover_at}` 变量，留空回退默认模板。
- README 新增「获取 QQ 邮箱 SMTP 授权码」图文步骤，以及「自定义邮件内容」变量说明与示例。

### v1.0.1（2026-10-10）

- **修复：邮件 From 头双重编码问题**。此前用 `str(Header(name, "utf-8"))` 嵌套 `formataddr` 会把发件人显示名二次编码，可能损坏邮件头导致服务端拒收或静默丢弃。现改为由 `email` 库按 RFC2047 正确编码。若此前测试邮件发送成功但收件箱无信，请更新后重新测试。
- `/掉线哨兵 测试` 的回复现在同时显示发件邮箱与收件邮箱，便于核对邮件去向。
- 本版本仅影响发信逻辑与指令回复，监控与告警行为不变。

### v1.0.0（2026-08-20）

- 首个版本：轮询 OneBot `get_status.online` 检测 QQ 登录状态。
- 连续离线超过阈值后通过 QQ 邮箱 SMTP 发送掉线告警邮件。
- 恢复在线后可选发送恢复通知邮件。
- 同一次掉线仅告警一次，状态持久化，重启不重复发送。
- 支持多 QQ 账号监控；提供状态查询与测试邮件指令。
