"""AstrBot 插件：QQ 掉线哨兵（astrbot_plugin_qq_offline_alarm）

通过 aiocqhttp(OneBot v11) 客户端轮询 NapCat 的 get_status 接口，
根据返回的 online 字段判断 QQ 登录状态：

- 连续离线超过 offline_threshold_seconds 后，通过 QQ 邮箱 SMTP 发送掉线告警邮件；
- QQ 恢复在线后，可选发送恢复通知邮件；
- 同一次掉线只发送一封告警邮件（状态持久化，重启不重复轰炸）。

创作者：小星萤
仅支持 aiocqhttp 适配器（协议端为 NapCat 等 OneBot v11 实现）。
"""

import asyncio
import inspect
import json
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_qq_offline_alarm"
DATA_DIR = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
STATE_PATH = DATA_DIR / "state.json"

# 邮件时间统一按北京时间展示，与部署环境时区无关。
try:
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _fmt_ts(dt: Optional[datetime]) -> str:
    if not dt:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_id_list(value: object) -> set[str]:
    """接受英文逗号、中文逗号、分号、空格或换行分隔的 ID 列表。"""
    return {item for item in re.findall(r"\d+", str(value or "")) if item}


def _split_emails(value: object) -> list[str]:
    """解析收件邮箱列表（逗号、分号、空格、换行分隔），并做基础格式过滤。"""
    raw = re.split(r"[,，;；\s]+", str(value or "").strip())
    emails: list[str] = []
    for item in raw:
        item = item.strip().strip("<>").strip()
        if item and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", item):
            emails.append(item)
    return emails


class QQOfflineAlarmPlugin(Star):
    """QQ 登录状态监控 + 邮箱告警。"""

    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config or {}
        self._closed = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._state_lock = asyncio.Lock()
        # 每个平台实例(QQ 号)一个监控状态
        self._state: dict[str, dict[str, Any]] = {}

    # -------------------- 配置读取 --------------------
    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        return value if isinstance(value, bool) else default

    def _cfg_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return str(value or "").strip()

    def _enabled(self) -> bool:
        return self._cfg_bool("enabled", True)

    def _check_interval(self) -> int:
        return self._cfg_int("check_interval_seconds", 30, 5, 600)

    def _offline_threshold(self) -> int:
        return self._cfg_int("offline_threshold_seconds", 90, 5, 3600)

    def _platform_ids(self) -> set[str]:
        return _parse_id_list(self.config.get("platform_ids", ""))

    def _notify_emails(self) -> list[str]:
        return _split_emails(self.config.get("notify_emails", ""))

    def _admins(self) -> set[str]:
        return _parse_id_list(self.config.get("admin_list", ""))

    def _smtp_conf(self) -> dict[str, Any]:
        return {
            "host": self._cfg_str("smtp_host", "smtp.qq.com"),
            "port": self._cfg_int("smtp_port", 465, 1, 65535),
            "ssl": self._cfg_bool("smtp_ssl", True),
            "user": self._cfg_str("smtp_user"),
            "auth_code": self._cfg_str("smtp_auth_code"),
            "from_name": self._cfg_str("smtp_from_name"),
        }

    # -------------------- 生命周期 --------------------
    async def initialize(self):
        if self._monitor_task and not self._monitor_task.done():
            return
        self._closed = False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        await self._load_state()
        if not self._enabled():
            logger.info("[qq_offline_alarm] 插件已加载，但未启用（enabled=false）")
            return
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="qq_offline_alarm_monitor"
        )
        emails = self._notify_emails()
        if not emails:
            logger.warning(
                "[qq_offline_alarm] notify_emails 为空，掉线时将不会发送邮件，"
                "请在插件配置中填写接收邮箱"
            )
        logger.info(
            f"[qq_offline_alarm] 监控已启动：间隔 {self._check_interval()}s，"
            f"掉线阈值 {self._offline_threshold()}s，收件人 {len(emails)} 个"
        )

    async def terminate(self):
        self._closed = True
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._monitor_task = None
        await self._save_state()
        logger.info("[qq_offline_alarm] 插件已停止")

    # -------------------- 状态持久化 --------------------
    async def _load_state(self):
        if not STATE_PATH.is_file():
            return
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
                self._state = {
                    str(k): v
                    for k, v in data["accounts"].items()
                    if isinstance(v, dict)
                }
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[qq_offline_alarm] 状态文件读取失败，将使用空状态: {exc}")

    async def _save_state(self):
        async with self._state_lock:
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                temp_path = STATE_PATH.with_suffix(".tmp")
                content = json.dumps(
                    {"accounts": self._state}, ensure_ascii=False, indent=2
                )
                temp_path.write_text(content, encoding="utf-8")
                temp_path.replace(STATE_PATH)
            except OSError as exc:
                logger.error(f"[qq_offline_alarm] 状态文件保存失败: {exc}")

    def _state_for(self, self_id: str) -> dict[str, Any]:
        record = self._state.setdefault(self_id, {})
        record.setdefault("online", None)
        record.setdefault("offline_since", None)  # iso 字符串
        record.setdefault("alerted", False)
        record.setdefault("last_alert_at", None)
        record.setdefault("nickname", "")
        return record

    # -------------------- 平台 / 客户端获取 --------------------
    @staticmethod
    async def _platform_client(platform: Any) -> Any:
        if platform is None:
            return None
        meta = getattr(platform, "meta", None)
        try:
            meta_obj = meta() if callable(meta) else meta
            if isinstance(meta_obj, dict):
                platform_name = str(meta_obj.get("name", "") or "").lower()
            else:
                platform_name = str(getattr(meta_obj, "name", "") or "").lower()
        except Exception:
            platform_name = ""
        if platform_name and platform_name != "aiocqhttp":
            return None
        get_client = getattr(platform, "get_client", None)
        client = get_client() if callable(get_client) else None
        if inspect.isawaitable(client):
            client = await client
        return client or getattr(platform, "bot", None)

    async def _all_aiocqhttp_clients(self) -> list[tuple[str, Any]]:
        """返回 [(platform_id, client)]，按配置的 platform_ids 过滤。"""
        wanted = self._platform_ids()
        manager = getattr(self.context, "platform_manager", None)
        get_insts = getattr(manager, "get_insts", None)
        platforms = get_insts() if callable(get_insts) else []
        if inspect.isawaitable(platforms):
            platforms = await platforms
        results: list[tuple[str, Any]] = []
        for platform in platforms or []:
            client = await self._platform_client(platform)
            if client is None:
                continue
            pid = str(getattr(platform, "unique_id", "") or getattr(platform, "id", ""))
            if not pid:
                meta = getattr(platform, "meta", None)
                try:
                    meta_obj = meta() if callable(meta) else meta
                    if isinstance(meta_obj, dict):
                        pid = str(meta_obj.get("name", ""))
                    else:
                        pid = str(getattr(meta_obj, "name", ""))
                except Exception:
                    pid = ""
            if wanted and pid not in wanted:
                continue
            results.append((pid or "aiocqhttp", client))
        return results

    @staticmethod
    async def _call(client: Any, action: str, **payload: Any) -> Any:
        api = getattr(client, "api", None)
        if api is None:
            return None
        call_action = getattr(api, "call_action", None)
        if call_action is None:
            return None
        result = call_action(action, **payload)
        if inspect.isawaitable(result):
            result = await result
        # OneBot 统一返回 { status, retcode, data, ... }，取出 data
        if isinstance(result, dict) and "data" in result and (
            "status" in result or "retcode" in result
        ):
            return result.get("data")
        return result

    # -------------------- 监控主循环 --------------------
    async def _monitor_loop(self):
        while not self._closed:
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"[qq_offline_alarm] 监控检查异常: {exc}")
            await asyncio.sleep(self._check_interval())

    async def _check_once(self):
        clients = await self._all_aiocqhttp_clients()
        if not clients:
            logger.debug("[qq_offline_alarm] 当前没有可用的 aiocqhttp 平台连接")
            return
        for pid, client in clients:
            try:
                await self._check_account(pid, client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    f"[qq_offline_alarm] 平台 {pid} 状态检查失败（可能连接中断）: {exc}"
                )
        await self._save_state()

    async def _check_account(self, pid: str, client: Any) -> None:
        # 先获取账号身份（登录号），失败则跳过本轮
        info = await self._call(client, "get_login_info")
        if not isinstance(info, dict):
            logger.warning(
                f"[qq_offline_alarm] 平台 {pid} get_login_info 失败，跳过本轮"
            )
            return
        self_id = str(info.get("user_id") or pid)
        nickname = str(info.get("nickname") or "")
        record = self._state_for(self_id)
        record["nickname"] = nickname

        status = await self._call(client, "get_status")
        online = (
            bool(status.get("online"))
            if isinstance(status, dict) and "online" in status
            else None
        )
        if online is None:
            logger.warning(
                f"[qq_offline_alarm] 平台 {pid}({self_id}) get_status 无 online 字段，"
                f"返回: {str(status)[:200]}"
            )
            return

        now = _now()
        now_iso = now.isoformat(timespec="seconds")
        record["last_check_at"] = now_iso

        if online:
            await self._handle_online(self_id, record, now)
        else:
            await self._handle_offline(self_id, record, now)

    async def _handle_online(self, self_id: str, record: dict, now: datetime) -> None:
        was_offline = record.get("online") is False
        record["online"] = True
        if was_offline and record.get("alerted"):
            # 之前掉线并已告警，现在恢复
            offline_since = record.get("offline_since")
            duration = self._duration_text(offline_since, now)
            record["recovered_at"] = now.isoformat(timespec="seconds")
            logger.info(f"[qq_offline_alarm] QQ {self_id} 已恢复在线")
            if self._cfg_bool("notify_on_recover", True):
                await self._send_mail_async(
                    subject=f"[QQ恢复通知] QQ {self_id} 已恢复在线",
                    body=(
                        f"QQ 账号已恢复在线。\n\n"
                        f"QQ 号：{self_id}\n"
                        f"昵称：{record.get('nickname', '')}\n"
                        f"恢复时间：{_fmt_ts(now)}\n"
                        f"离线时长：{duration}"
                    ),
                )
            # 重置状态，为下一次掉线重新武装
            record["offline_since"] = None
            record["alerted"] = False
        elif record.get("online") is not True or record.get("offline_since"):
            # 恢复状态但未告警（阈值未到就恢复），静默重置
            record["offline_since"] = None
            record["alerted"] = False

    async def _handle_offline(self, self_id: str, record: dict, now: datetime) -> None:
        if record.get("online") is not False:
            # 首次检测到掉线，开始计时
            record["online"] = False
            record["offline_since"] = now.isoformat(timespec="seconds")
            logger.info(f"[qq_offline_alarm] QQ {self_id} 检测到离线，开始计时")
        if record.get("alerted"):
            return  # 同一次掉线已告警
        offline_since = record.get("offline_since")
        if not offline_since:
            return
        try:
            since = datetime.fromisoformat(offline_since)
        except (TypeError, ValueError):
            return
        if (now - since).total_seconds() < self._offline_threshold():
            return
        record["alerted"] = True
        record["last_alert_at"] = now.isoformat(timespec="seconds")
        logger.warning(
            f"[qq_offline_alarm] QQ {self_id} 持续离线超过阈值，发送掉线告警邮件"
        )
        await self._send_mail_async(
            subject=f"[QQ掉线提醒] QQ {self_id} 已掉线",
            body=(
                f"QQ 账号检测到掉线！\n\n"
                f"QQ 号：{self_id}\n"
                f"昵称：{record.get('nickname', '')}\n"
                f"检测到离线时间：{_fmt_ts(since)}\n"
                f"当前时间：{_fmt_ts(now)}\n"
                f"持续离线：{self._duration_text(offline_since, now)}\n\n"
                f"请尽快检查 NapCat / QQ 登录状态。"
            ),
        )

    @staticmethod
    def _duration_text(offline_since: Optional[str], now: datetime) -> str:
        if not offline_since:
            return "-"
        try:
            since = datetime.fromisoformat(offline_since)
            total = max(0, int((now - since).total_seconds()))
            hours, rem = divmod(total, 3600)
            minutes, seconds = divmod(rem, 60)
            if hours:
                return f"{hours} 小时 {minutes} 分 {seconds} 秒"
            return f"{minutes} 分 {seconds} 秒"
        except (TypeError, ValueError):
            return "-"

    # -------------------- 邮件发送 --------------------
    def _smtp_send_sync(self, subject: str, body: str) -> None:
        conf = self._smtp_conf()
        if not conf["host"] or not conf["user"] or not conf["auth_code"]:
            raise RuntimeError("SMTP 配置不完整：smtp_host / smtp_user / smtp_auth_code 均不能为空")
        from_addr = conf["user"]
        to_addrs = self._notify_emails()
        if not to_addrs:
            raise RuntimeError("notify_emails 为空，未发送任何邮件")
        display = conf["from_name"] or from_addr
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header(display, "utf-8")), from_addr))
        msg["To"] = ", ".join(to_addrs)
        timeout = 20
        if conf["ssl"]:
            with smtplib.SMTP_SSL(conf["host"], conf["port"], timeout=timeout) as server:
                server.login(conf["user"], conf["auth_code"])
                server.sendmail(from_addr, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(conf["host"], conf["port"], timeout=timeout) as server:
                server.starttls()
                server.login(conf["user"], conf["auth_code"])
                server.sendmail(from_addr, to_addrs, msg.as_string())

    async def _send_mail_async(self, subject: str, body: str) -> tuple[bool, str]:
        try:
            await asyncio.to_thread(self._smtp_send_sync, subject, body)
            logger.info(f"[qq_offline_alarm] 邮件发送成功：{subject}")
            return True, "发送成功"
        except smtplib.SMTPAuthenticationError:
            logger.error("[qq_offline_alarm] SMTP 认证失败：请检查邮箱授权码是否正确")
            return False, "SMTP 认证失败：授权码不正确或未开启 SMTP 服务"
        except smtplib.SMTPException as exc:
            logger.error(f"[qq_offline_alarm] SMTP 发送失败: {exc}")
            return False, f"SMTP 发送失败：{exc}"
        except Exception as exc:
            logger.exception(f"[qq_offline_alarm] 邮件发送异常: {exc}")
            return False, f"邮件发送异常：{exc}"

    # -------------------- 指令 --------------------
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admins = self._admins()
        if not admins:
            return False
        return str(event.get_sender_id()) in admins

    def _smtp_summary(self) -> str:
        conf = self._smtp_conf()
        if not conf["user"] or not conf["auth_code"]:
            return "未配置（smtp_user / smtp_auth_code）"
        user = conf["user"]
        masked = user[:3] + "***" + user[user.find("@"):] if "@" in user else "***"
        return f"{masked} @ {conf['host']}:{conf['port']}"

    def _accounts_lines(self) -> str:
        if not self._state:
            return "暂无监控数据（等待首次检查）"
        lines = []
        for self_id in sorted(self._state):
            record = self._state[self_id]
            online = record.get("online")
            if online is True:
                status = "在线"
            elif online is False:
                status = "离线"
            else:
                status = "未知"
            nickname = record.get("nickname", "")
            last = _fmt_ts(self._parse_dt(record.get("last_check_at")))
            if record.get("alerted"):
                status += "（已告警）"
            name_part = f"（{nickname}）" if nickname else ""
            lines.append(f"QQ {self_id}{name_part}：{status}｜上次检查 {last}")
        return "\n".join(lines)

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    @filter.command_group("掉线哨兵", alias={"offline"})
    def offline_group(self):
        pass

    @offline_group.command("状态")
    async def status_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(
            "QQ 掉线哨兵状态\n"
            "————————————\n"
            f"{self._accounts_lines()}\n"
            f"检测间隔：{self._check_interval()} 秒\n"
            f"掉线阈值：{self._offline_threshold()} 秒\n"
            f"收件邮箱：{len(self._notify_emails())} 个\n"
            f"SMTP：{self._smtp_summary()}\n"
            f"恢复通知：{'开' if self._cfg_bool('notify_on_recover', True) else '关'}\n"
            f"监控开关：{'开' if self._enabled() else '关'}"
        )

    @offline_group.command("测试")
    async def test_cmd(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("没有权限：请在 admin_list 中配置你的 QQ 号")
            return
        conf = self._smtp_conf()
        if not conf["user"] or not conf["auth_code"]:
            yield event.plain_result(
                "SMTP 未配置完整，无法发送测试邮件。请在插件配置中填写 smtp_user 与 smtp_auth_code。"
            )
            return
        if not self._notify_emails():
            yield event.plain_result("notify_emails 为空，请先配置接收邮箱")
            return
        ok, message = await self._send_mail_async(
            subject="[测试] QQ 掉线哨兵 SMTP 配置测试",
            body=(
                "这是一封测试邮件，说明 QQ 掉线哨兵的 SMTP 配置可用。\n\n"
                f"发送时间：{_fmt_ts(_now())}"
            ),
        )
        yield event.plain_result(f"测试邮件：{message}")

    @offline_group.command("帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(
            "QQ 掉线哨兵\n"
            "————————————\n"
            "监控 NapCat 的 QQ 登录状态，掉线超过阈值后通过 QQ 邮箱提醒。\n"
            "指令：\n"
            "  /掉线哨兵 状态  查看各 QQ 在线状态与配置摘要\n"
            "  /掉线哨兵 测试  发送一封测试邮件（需管理员）\n"
            "  /掉线哨兵 帮助  显示本帮助\n"
            "掉线邮件与恢复邮件自动发送，无需人工干预。"
        )
