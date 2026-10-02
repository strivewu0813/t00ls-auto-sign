#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T00ls 每日自动签到脚本（Ubuntu / 任意 Linux 发行版，Python 3.8+）

官方接口文档：https://www.t00ls.com/api.html
  POST /login.json            action=login & username & password(MD5) & questionid & answer
  GET  /members-profile.json  返回 memberinfo.formhash / sign_today / sign_times / extcredits2
  POST /ajax-sign.json        formhash & signsubmit=true

幂等设计：脚本每次都先查 sign_today，已签到就直接退出（退出码 0），
因此可以放心地把定时任务设置成一天跑多次，失败时自然形成重试。

用法：
  python3 t00ls_sign.py --config config.ini              # 签到（幂等）
  python3 t00ls_sign.py --config config.ini --check      # 只查状态，不签到
  python3 t00ls_sign.py --config config.ini --dry-run    # 演练：登录+查状态，不提交签到
  python3 t00ls_sign.py --config config.ini -v           # 输出调试日志

退出码：0 = 成功或今日已签到；1 = 签到/接口/网络失败；2 = 配置或参数错误
"""

from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import hmac
import json
import logging
import os
import random
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少依赖 requests，请先安装：pip3 install requests\n")
    sys.exit(2)

__version__ = "1.3.0"

DEFAULT_BASE_URL = "https://www.t00ls.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

QUESTION_HELP = """安全提问编号 questionid：
  0 = 没有安全提问
  1 = 母亲的名字
  2 = 爷爷的名字
  3 = 父亲出生的城市
  4 = 您其中一位老师的名字
  5 = 您个人计算机的型号
  6 = 您最喜欢的餐馆名称
  7 = 驾驶执照的最后四位数字"""

LOGGER = logging.getLogger("t00ls")


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class T00lsError(Exception):
    """接口或网络层错误。"""


class ConfigError(T00lsError):
    """配置错误。"""


class AuthError(T00lsError):
    """登录 / 身份认证失败。"""


class SignResult(object):
    def __init__(self, ok: bool, already: bool, message: str, raw: Any = None) -> None:
        self.ok = ok
        self.already = already
        self.message = message
        self.raw = raw


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def collect_strings(obj: Any, out: Optional[List[str]] = None) -> List[str]:
    """把嵌套 JSON 里的所有标量值收集成字符串列表（接口字段名在历史上变过，故做模糊匹配）。"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for value in obj.values():
            collect_strings(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            collect_strings(value, out)
    elif obj is not None:
        out.append(str(obj))
    return out


def normalize_cookie(raw: str) -> str:
    """把浏览器里复制出来的 Cookie 整理成一行 Cookie 头。"""
    cookie = (raw or "").strip()
    if not cookie:
        return ""
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:]
    # 支持多行粘贴（每行一个 name=value）
    cookie = " ".join(line.strip() for line in cookie.splitlines() if line.strip())
    parts: List[str] = []
    for chunk in cookie.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        if chunk not in parts:
            parts.append(chunk)
    return "; ".join(parts)


def mask(text: str, keep: int = 4) -> str:
    text = str(text)
    if len(text) <= keep:
        return "*" * len(text)
    return text[:keep] + "*" * (len(text) - keep)


# 论坛按北京时间（UTC+8）划分"今天"，无论服务器在哪个时区都要按这个算
CST = timezone(timedelta(hours=8))


def date_cst(offset_days: int = 0) -> str:
    return (datetime.now(CST) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def today_cst() -> str:
    return date_cst(0)


def _previous_day(day: str) -> str:
    """给定 YYYY-MM-DD 返回前一天；解析失败返回空串。"""
    try:
        return (datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def days_between(earlier: Optional[str], later: str) -> Optional[int]:
    """两个 YYYY-MM-DD 之间相差的天数；解析失败返回 None。"""
    try:
        start = datetime.strptime(str(earlier), "%Y-%m-%d").date()
        end = datetime.strptime(str(later), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (end - start).days


def as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def looks_like_stale_sign_today(state: "State", info: Dict[str, Any], today: str) -> bool:
    """判断 sign_today=1 是否是"站点还没换日"留下的昨天状态。

    典型场景：定时任务设在北京时间 00:05，而站点在 00:00 之后要过一会儿才把
    签到状态清零。此时接口仍返回 sign_today=1，脚本若直接当成"今天已签到"就会
    一整天都不再签到（静默漏签）。

    依据两个条件同时成立才怀疑：
      1) 我们上次确认签到的日期就是昨天（北京时间的昨天）；
      2) 站点的累计签到次数与那时完全相同 —— 说明今天还没有产生新的签到记录。
    站点把 App/公众号/TG/网页的签到都算在同一个 sign_times/sign_today 上，
    所以"计数没变"基本可以断定今天确实还没签。
    """
    if state.get("last_sign_date") != _previous_day(today):
        return False
    last_times = as_int(state.get("last_sign_times"))
    now_times = as_int(info.get("sign_times"))
    if last_times is None or now_times is None:
        return False
    return now_times == last_times


def resolve_stale_sign_today(
    client: T00lsClient,
    today: str,
    info: Dict[str, Any],
    retries: int,
    wait: float,
) -> Dict[str, Any]:
    """怀疑站点还没换日：等一会儿再签，成功则返回新的 info，否则抛错交给上层报失败。

    直接调签到接口是安全的：真没换日时接口只会回 alreadysign，不会重复计数。
    """
    LOGGER.warning(
        "sign_today=1 但累计签到次数与昨天相同，站点很可能还没换日（昨天的残留状态）；"
        "将等待 %g 秒后重试签到，最多 %d 次",
        wait, retries,
    )
    for attempt in range(1, retries + 1):
        if wait > 0:
            time.sleep(wait)
        result = client.sign()
        if result.ok and not result.already:
            LOGGER.info("站点已换日：第 %d 次尝试签到成功", attempt)
            time.sleep(1.5)
            refreshed = client.profile()
            if not T00lsClient.is_signed_today(refreshed):
                raise T00lsError("签到接口返回成功，但 sign_today 仍为 0，请手动确认")
            return refreshed
        LOGGER.info("第 %d/%d 次尝试：接口仍返回 alreadysign（站点尚未换日）", attempt, retries)
    raise T00lsError(
        "站点似乎还没换日（sign_today 一直是昨天的状态），本次未能签到；"
        "建议在 00:05 之后再安排一次触发，或调大 --stale-retries"
    )


# --------------------------------------------------------------------------- #
# 本地状态：判断"中间是否漏签"，以及保证每天最多补签一次
# --------------------------------------------------------------------------- #
class State(object):
    """只记录两类事实：最后一次「确认已签到」的日期/累计次数，以及补签尝试记录。

    累计签到次数（sign_times）来自站点而不是我们的记录，所以即使脚本停跑几天，
    只要下次跑起来就能算出中间漏签了几天，不会因为本机宕机而误判。
    """

    VERSION = 1

    def __init__(self, path: str) -> None:
        self.path = path
        self.data: Dict[str, Any] = {}

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.data = data
        except (OSError, ValueError) as exc:
            LOGGER.warning("状态文件读取失败（按首次运行处理）：%s", exc)

    def save(self) -> None:
        if not self.path:
            return
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            self.data["version"] = self.VERSION
            temp = self.path + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(temp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            LOGGER.warning("状态文件写入失败：%s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def record_signed(self, today: str, sign_times: Optional[int], tubi: Optional[int]) -> None:
        """只在「确认今天已签到」时调用，保证基线日期与计数是同一时刻的。"""
        self.data["last_sign_date"] = today
        if sign_times is not None:
            self.data["last_sign_times"] = sign_times
        if tubi is not None:
            self.data["last_tubi"] = tubi

    def bu_sign_dates(self) -> List[str]:
        dates = self.data.get("bu_sign_dates")
        return list(dates) if isinstance(dates, list) else []

    def bu_sign_done_today(self, today: str) -> bool:
        return today in self.bu_sign_dates()

    def mark_bu_sign(self, today: str) -> None:
        dates = [d for d in self.bu_sign_dates() if d != today]
        dates.append(today)
        self.data["bu_sign_dates"] = dates[-30:]

    def detect_missed_days(self, today: str, sign_times: Optional[int]) -> Tuple[Optional[int], str]:
        """返回 (窗口内漏签天数, 说明)。None 表示缺少基线，无法判断。

        两个独立信号取较小值（宁可少补，也不冤枉花 20 TuBi）：
          counter  —— 站点累计签到次数的增量：若每天都签，增量应等于相隔天数
          gap      —— 我们自己记录里「没有签到过」的天数
        两者不一致通常意味着用户用 App/微信/TG 签过，此时以更保守的为准。
        """
        last_sign = self.get("last_sign_date")
        last_times = as_int(self.get("last_sign_times"))
        if not last_sign:
            return None, "首次运行，没有基线"
        elapsed = days_between(last_sign, today)
        if elapsed is None:
            return None, "状态里的日期无法解析：%s" % last_sign
        if elapsed <= 0:
            return 0, "今天已经记过账"
        gap_days = elapsed - 1
        if sign_times is None or last_times is None:
            return None, "站点未返回累计签到次数，无法安全判断漏签"
        delta = sign_times - last_times
        if delta < 0:
            return None, "累计签到次数异常变小（%s -> %s）" % (last_times, sign_times)
        counter_missed = elapsed - delta
        missed = max(0, min(counter_missed, gap_days))
        detail = "相隔 %d 天，期间累计签到 +%d（counter 认为漏 %d，记录认为漏 %d）" % (
            elapsed, delta, max(0, counter_missed), gap_days)
        return missed, detail


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class Config(object):
    def __init__(self) -> None:
        self.base_url: str = DEFAULT_BASE_URL
        self.username: str = ""
        self.password: str = ""
        self.password_md5: str = ""
        self.question_id: int = 0
        self.question_answer: str = ""
        self.cookie: str = ""
        self.timeout: float = 20.0
        self.retries: int = 3
        self.log_file: str = ""

        self.notify_enabled: bool = False
        self.notify_channels: List[str] = []
        self.notify_always: bool = False
        self.dingtalk_webhook: str = ""
        self.dingtalk_secret: str = ""
        self.wecom_webhook: str = ""
        self.serverchan_key: str = ""
        self.bark_url: str = ""
        self.telegram_token: str = ""
        self.telegram_chat_id: str = ""
        self.smtp_host: str = ""
        self.smtp_port: int = 465
        self.smtp_ssl: bool = True
        self.smtp_user: str = ""
        self.smtp_password: str = ""
        self.mail_from: str = ""
        self.mail_to: str = ""

        # 状态文件与自动补签
        self.state_file: str = ""
        self.bu_sign: bool = False
        self.bu_sign_within_days: int = 3
        # 站点尚未换日时的重试（针对北京 00:05 这类刚过零点的定时任务）
        self.stale_retries: int = 3
        self.stale_wait: float = 120.0

    # -- 派生 -------------------------------------------------------------- #
    def resolved_password_md5(self) -> str:
        """优先使用显式配置的 32 位 MD5，否则对明文密码做 MD5。"""
        if self.password_md5.strip():
            return self.password_md5.strip().lower()
        if self.password:
            return md5_hex(self.password)
        return ""

    @property
    def has_login(self) -> bool:
        return bool(self.cookie.strip()) or bool(
            self.username.strip() and self.resolved_password_md5()
        )


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "":
        return default
    return text in ("1", "true", "yes", "y", "on", "是")


def load_config(path: Optional[str]) -> Config:
    """读取 INI 配置；环境变量优先级更高（便于配合 systemd 的 EnvironmentFile）。"""
    cfg = Config()
    parser = configparser.ConfigParser()

    files: List[str] = []
    if path:
        files.append(path)
    elif os.environ.get("T00LS_CONFIG"):
        files.append(os.environ["T00LS_CONFIG"])
    else:
        guess = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
        if os.path.exists(guess):
            files.append(guess)

    if files:
        if not os.path.exists(files[0]):
            raise ConfigError("配置文件不存在：%s" % files[0])
        # utf-8-sig：兼容 Windows 记事本 / PowerShell 保存时带的 UTF-8 BOM
        try:
            with open(files[0], "r", encoding="utf-8-sig") as handle:
                parser.read_file(handle, source=files[0])
        except configparser.Error as exc:
            raise ConfigError("配置文件格式错误（%s）：%s" % (files[0], exc))
        except OSError as exc:
            raise ConfigError("无法读取配置文件 %s：%s" % (files[0], exc))
    else:
        LOGGER.debug("未找到配置文件，仅使用环境变量 / 命令行参数")

    def get(section: str, option: str, default: str = "") -> str:
        if parser.has_section(section) and parser.has_option(section, option):
            return parser.get(section, option).strip()
        return default

    cfg.base_url = get("t00ls", "base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
    cfg.username = get("t00ls", "username")
    cfg.password = get("t00ls", "password")
    cfg.password_md5 = get("t00ls", "password_md5")
    cfg.question_answer = get("t00ls", "question_answer")
    cfg.cookie = get("t00ls", "cookie")
    try:
        cfg.question_id = int(get("t00ls", "question_id", "0") or 0)
    except ValueError:
        raise ConfigError("question_id 必须是数字（0-7）")
    try:
        cfg.timeout = float(get("t00ls", "timeout", "20") or 20)
    except ValueError:
        raise ConfigError("timeout 必须是数字（秒）")
    try:
        cfg.retries = max(1, int(get("t00ls", "retries", "3") or 3))
    except ValueError:
        raise ConfigError("retries 必须是数字")
    cfg.log_file = get("t00ls", "log_file")
    cfg.state_file = get("t00ls", "state_file")
    cfg.bu_sign = _as_bool(get("t00ls", "auto_bu_sign", "false"))
    try:
        cfg.bu_sign_within_days = int(get("t00ls", "bu_sign_within_days", "3") or 3)
    except ValueError:
        raise ConfigError("bu_sign_within_days 必须是数字")
    try:
        cfg.stale_retries = int(get("t00ls", "stale_retries", "3") or 3)
    except ValueError:
        raise ConfigError("stale_retries 必须是数字")
    try:
        cfg.stale_wait = float(get("t00ls", "stale_wait", "120") or 120)
    except ValueError:
        raise ConfigError("stale_wait 必须是数字（秒）")

    cfg.notify_enabled = _as_bool(get("notify", "enabled", "false"))
    channels = get("notify", "channels")
    cfg.notify_channels = [c.strip().lower() for c in channels.replace("\n", ",").split(",") if c.strip()]
    cfg.notify_always = _as_bool(get("notify", "always", "false"))
    cfg.dingtalk_webhook = get("notify", "dingtalk_webhook")
    cfg.dingtalk_secret = get("notify", "dingtalk_secret")
    cfg.wecom_webhook = get("notify", "wecom_webhook")
    cfg.serverchan_key = get("notify", "serverchan_key")
    cfg.bark_url = get("notify", "bark_url")
    cfg.telegram_token = get("notify", "telegram_token")
    cfg.telegram_chat_id = get("notify", "telegram_chat_id")
    cfg.smtp_host = get("notify", "smtp_host")
    cfg.smtp_user = get("notify", "smtp_user")
    cfg.smtp_password = get("notify", "smtp_password")
    cfg.mail_from = get("notify", "mail_from") or cfg.smtp_user
    cfg.mail_to = get("notify", "mail_to")
    cfg.smtp_ssl = _as_bool(get("notify", "smtp_ssl", "true"), True)
    try:
        cfg.smtp_port = int(get("notify", "smtp_port", "465") or 465)
    except ValueError:
        raise ConfigError("smtp_port 必须是数字")

    # ---- 环境变量覆盖（secrets 可以不落盘） ---- #
    env_map = {
        "T00LS_BASE_URL": "base_url",
        "T00LS_USERNAME": "username",
        "T00LS_PASSWORD": "password",
        "T00LS_PASSWORD_MD5": "password_md5",
        "T00LS_QUESTION_ANSWER": "question_answer",
        "T00LS_COOKIE": "cookie",
        "T00LS_LOG_FILE": "log_file",
        "T00LS_STATE_FILE": "state_file",
        "T00LS_DINGTALK_WEBHOOK": "dingtalk_webhook",
        "T00LS_DINGTALK_SECRET": "dingtalk_secret",
        "T00LS_WECOM_WEBHOOK": "wecom_webhook",
        "T00LS_SERVERCHAN_KEY": "serverchan_key",
        "T00LS_BARK_URL": "bark_url",
        "T00LS_TELEGRAM_TOKEN": "telegram_token",
        "T00LS_TELEGRAM_CHAT_ID": "telegram_chat_id",
        "T00LS_SMTP_HOST": "smtp_host",
        "T00LS_SMTP_USER": "smtp_user",
        "T00LS_SMTP_PASSWORD": "smtp_password",
        "T00LS_MAIL_TO": "mail_to",
    }
    for env_name, attr in env_map.items():
        value = os.environ.get(env_name)
        if value:
            setattr(cfg, attr, value.strip())
    # 环境变量优先级高于配置文件：显式用 T00LS_PASSWORD 时，不能被配置文件里
    # 残留的 password_md5 悄悄压掉（反之亦然，MD5 更明确所以优先）。
    if os.environ.get("T00LS_PASSWORD") and not os.environ.get("T00LS_PASSWORD_MD5"):
        cfg.password_md5 = ""
    if os.environ.get("T00LS_QUESTION_ID"):
        try:
            cfg.question_id = int(os.environ["T00LS_QUESTION_ID"])
        except ValueError:
            raise ConfigError("环境变量 T00LS_QUESTION_ID 必须是数字")
    if os.environ.get("T00LS_NOTIFY_ENABLED"):
        cfg.notify_enabled = _as_bool(os.environ["T00LS_NOTIFY_ENABLED"])
    if os.environ.get("T00LS_NOTIFY_CHANNELS"):
        cfg.notify_channels = [
            c.strip().lower()
            for c in os.environ["T00LS_NOTIFY_CHANNELS"].replace("\n", ",").split(",")
            if c.strip()
        ]
    if os.environ.get("T00LS_AUTO_BU_SIGN"):
        cfg.bu_sign = _as_bool(os.environ["T00LS_AUTO_BU_SIGN"])

    if not cfg.state_file:
        base_dir = (
            os.path.dirname(os.path.abspath(files[0]))
            if files
            else os.path.dirname(os.path.abspath(__file__))
        )
        cfg.state_file = os.path.join(base_dir, "t00ls-state.json")

    if cfg.question_id not in range(0, 8):
        LOGGER.warning("question_id=%s 不在 0-7 范围内，请确认是否填写正确", cfg.question_id)
    return cfg


def setup_logging(verbose: bool, log_file: str) -> None:
    # 服务器上常见的 LANG=C / POSIX locale 会让中文与表情写入 stdout 时抛
    # UnicodeEncodeError，这里统一放宽为 UTF-8 + 容错，避免脚本因日志而中断。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    level = logging.DEBUG if verbose else logging.INFO
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            directory = os.path.dirname(os.path.abspath(log_file))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            sys.stderr.write("警告：无法写入日志文件 %s（%s），仅输出到标准输出\n" % (log_file, exc))

    # 先清掉已有 handler：logging.basicConfig 在 root 已有 handler 时是空操作，
    # 会让"同一进程里第二次调用 main()"的日志继续写向旧的输出流（重复 handler 也会重复打印）。
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


# --------------------------------------------------------------------------- #
# T00ls 客户端
# --------------------------------------------------------------------------- #
class T00lsClient(object):
    # login.json 的真实返回（2026-10 实测）：
    #   {"status":"fail","message":1}  密码为空
    #   {"status":"fail","message":2}  用户名为空
    #   {"status":"fail","message":3}  用户名或密码不对
    #   {"status":"fail","message":4}  用户名或密码不对（通用失败码）
    #   {"status":"fail","message":"5failedlogin,plswait15min"}  同 IP 连续失败过多
    # 这里把它们翻译成人话，未知提示原样展示。
    LOGIN_HINTS = {
        "1": "密码为空：检查 config.ini 的 password / password_md5",
        "2": "用户名为空：检查 config.ini 的 username",
        "3": "用户名或密码不对，也可能是安全提问（question_id/answer）不匹配",
        "4": "用户名、密码或安全提问不正确",
        "5": "同一 IP 连续登录失败次数过多，站点已限制登录，请等约 15 分钟后再试",
        "login_invalid": "用户名或密码（MD5）不对",
        "login_question_invalid": "安全提问错误：question_id / question_answer 不匹配",
        "login_answer_invalid": "安全提问答案错误",
        "login_strike": "密码错误次数过多，账号被临时锁定，请稍后再试",
        "login_seccheck": "站点要求安全验证（验证码），请改用 cookie 方式",
        "login_nonexistence": "账号不存在",
        "submit_seccode_invalid": "验证码错误",
        "wrongaction": "接口没收到 action=login：多半是域名跳转把 POST 降级成了 GET，"
                       "请把 config.ini 的 base_url 改成 https://www.t00ls.com",
    }
    LOGIN_HINT_PATTERNS = (
        ("failedlogin", "同一 IP 连续登录失败次数过多，站点已限制登录，请等约 15 分钟后再试"),
        ("plswait", "站点要求稍后再试（连续失败被限制）"),
        ("seccheck", "站点要求安全验证（验证码），请改用 cookie 方式"),
    )

    @classmethod
    def _login_hint(cls, message: str) -> str:
        if message in cls.LOGIN_HINTS:
            return cls.LOGIN_HINTS[message]
        lowered = message.lower()
        for pattern, hint in cls.LOGIN_HINT_PATTERNS:
            if pattern in lowered:
                return hint
        return ""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": cfg.base_url.rstrip("/") + "/",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        self.formhash: Optional[str] = None
        self.username: str = cfg.username

    # -- 底层请求 ---------------------------------------------------------- #
    def _url(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    def _canonical_origin(self, response: requests.Response) -> Optional[str]:
        """跨域跳转时返回新的 origin（如 www.t00ls.net -> www.t00ls.com），否则 None。"""
        try:
            final = urlsplit(response.url)
            base = urlsplit(self.cfg.base_url)
        except ValueError:
            return None
        if not final.scheme or not final.netloc:
            return None
        if (final.scheme, final.netloc) == (base.scheme, base.netloc):
            return None
        return "%s://%s" % (final.scheme, final.netloc)

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.cfg.retries + 1):
            try:
                response = self.session.request(
                    method, self._url(path), timeout=self.cfg.timeout, **kwargs
                )
                if response.status_code >= 500:
                    raise requests.HTTPError("HTTP %s" % response.status_code, response=response)

                # 301/302 会让 requests 把 POST 降级成 GET（请求体被丢掉），
                # 结果就是接口回一个莫名其妙的 wrongaction。这里自动改用跳转后的
                # 真实域名重发，并记住它，后续请求都直连（最多跟 3 跳，防环）。
                for _ in range(3):
                    new_origin = self._canonical_origin(response)
                    if not new_origin:
                        break
                    LOGGER.warning(
                        "接口域名 %s 跳转到了 %s，已自动改用新域名重发请求"
                        "（建议把 config.ini 的 base_url 改成 %s）",
                        self.cfg.base_url, new_origin, new_origin,
                    )
                    self.cfg.base_url = new_origin
                    self.session.headers["Referer"] = new_origin + "/"
                    response = self.session.request(
                        method, self._url(path), timeout=self.cfg.timeout, **kwargs
                    )
                    if response.status_code >= 500:
                        raise requests.HTTPError("HTTP %s" % response.status_code, response=response)

                LOGGER.debug("%s %s -> HTTP %s", method, path, response.status_code)
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.cfg.retries:
                    break
                wait = min(2 ** attempt, 20) + random.uniform(0, 2)
                LOGGER.warning(
                    "请求 %s 失败（第 %d/%d 次）：%s；%.1f 秒后重试",
                    path, attempt, self.cfg.retries, exc, wait,
                )
                time.sleep(wait)
        raise T00lsError("请求 %s 连续 %d 次失败：%s" % (path, self.cfg.retries, last_error))

    def _json(self, response: requests.Response) -> Dict[str, Any]:
        text = (response.text or "").strip()
        try:
            data = response.json()
        except ValueError:
            raise T00lsError(
                "接口未返回 JSON（HTTP %s，可能被 CDN/WAF 拦截或域名变更）：%s"
                % (response.status_code, text[:200])
            )
        if not isinstance(data, dict):
            raise T00lsError("接口返回结构异常：%s" % str(data)[:200])
        return data

    def close(self) -> None:
        self.session.close()

    # -- 认证 -------------------------------------------------------------- #
    def login(self) -> Dict[str, Any]:
        """优先使用 cookie（若配置了），否则用户名 + MD5 密码 + 安全提问登录。"""
        if self.cfg.cookie.strip():
            return self.login_with_cookie(self.cfg.cookie)

        if not self.cfg.username.strip():
            raise ConfigError("未配置 username（或改用 cookie 方式）")
        password_md5 = self.cfg.resolved_password_md5()
        if not password_md5:
            raise ConfigError("未配置 password / password_md5")

        payload = self._json(
            self._request(
                "POST",
                "/login.json",
                data={
                    "action": "login",
                    "username": self.cfg.username,
                    "password": password_md5,
                    "questionid": str(self.cfg.question_id),
                    "answer": self.cfg.question_answer,
                },
            )
        )
        if str(payload.get("status")) != "success":
            message = str(payload.get("message") or payload)
            hint = self._login_hint(message)
            raise AuthError("登录失败：%s%s" % (message, "（%s）" % hint if hint else ""))
        self.formhash = (payload.get("formhash") or "").strip() or None
        self.username = self.cfg.username
        LOGGER.info("登录成功：%s", self.username)
        return payload

    def login_with_cookie(self, raw_cookie: str) -> Dict[str, Any]:
        cookie = normalize_cookie(raw_cookie)
        if not cookie:
            raise ConfigError("cookie 为空")
        self.session.headers["Cookie"] = cookie
        LOGGER.info("使用 Cookie 认证（%s…）", mask(cookie, 12))
        info = self.profile()  # 无效 cookie 会抛 AuthError
        self.username = info.get("username") or self.username or "cookie用户"
        LOGGER.info("Cookie 有效，当前用户：%s", self.username)
        return info

    # -- 业务 -------------------------------------------------------------- #
    def profile(self) -> Dict[str, Any]:
        payload = self._json(self._request("GET", "/members-profile.json"))
        if str(payload.get("status")) != "success":
            message = str(payload.get("message") or payload)
            if message == "loginfirst":
                raise AuthError("Cookie 已失效或未登录（loginfirst），请重新获取 Cookie")
            raise T00lsError("获取用户信息失败：%s" % message)
        info = payload.get("memberinfo") or {}
        if not isinstance(info, dict):
            raise T00lsError("用户信息字段异常：%s" % str(info)[:200])
        if not self.formhash:
            self.formhash = (info.get("formhash") or "").strip() or None
        if not self.username:
            self.username = info.get("username") or ""
        return info

    @staticmethod
    def is_signed_today(info: Dict[str, Any]) -> bool:
        return str(info.get("sign_today", "0")).strip() in ("1", "true", "True")

    def _submit_sign(self) -> SignResult:
        if not self.formhash:
            self.profile()
        if not self.formhash:
            raise T00lsError("无法获取 formhash，签到中止")
        payload = self._json(
            self._request(
                "POST",
                "/ajax-sign.json",
                data={"formhash": self.formhash, "signsubmit": "true"},
            )
        )
        strings = " ".join(collect_strings(payload)).lower()
        if "alreadysign" in strings:
            return SignResult(True, True, "今日已签到（alreadysign）", payload)
        if str(payload.get("status")).lower() == "success":
            return SignResult(True, False, "签到成功", payload)
        return SignResult(False, False, str(payload.get("message") or payload), payload)

    def sign(self) -> SignResult:
        """提交签到；formhash 失效（wrongsubmit）时自动刷新一次再试。"""
        result = self._submit_sign()
        if result.ok:
            return result
        if "wrongsubmit" in result.message.lower() or "formhash" in result.message.lower():
            LOGGER.warning("formhash 失效（%s），刷新后重试一次", result.message)
            self.formhash = None
            self.profile()
            result = self._submit_sign()
            if result.ok:
                return result
        raise T00lsError("签到失败：%s" % result.message)

    def bu_sign(self) -> SignResult:
        """补签：POST /ajax-busign.json（官方文档：formhash + signsubmit=true，每次扣 20 TuBi）。

        官方文档只给了参数、没给完整的返回值语义，所以这里把原始返回一并带回去写进日志，
        方便用自己的账号核对一次真实行为。
        """
        if not self.formhash:
            self.profile()
        if not self.formhash:
            raise T00lsError("无法获取 formhash，补签中止")
        payload = self._json(
            self._request(
                "POST",
                "/ajax-busign.json",
                data={"formhash": self.formhash, "signsubmit": "true"},
            )
        )
        strings = " ".join(collect_strings(payload)).lower()
        if "alreadysign" in strings:
            return SignResult(True, True, "今日已签到（alreadysign）", payload)
        if "wrongbusubmit" in strings:
            return SignResult(False, False, "接口返回 wrongbusubmit（没有可补签的日期）", payload)
        if str(payload.get("status")).lower() == "success":
            return SignResult(True, False, "补签请求已提交", payload)
        return SignResult(False, False, str(payload.get("message") or payload), payload)


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
class Notifier(object):
    # 每个渠道必需哪些配置项；缺了就在启动时跳过，而不是等到签到成功后才报错
    REQUIRED_FIELDS = {
        "dingtalk": ("dingtalk_webhook",),
        "wecom": ("wecom_webhook",),
        "serverchan": ("serverchan_key",),
        "bark": ("bark_url",),
        "telegram": ("telegram_token", "telegram_chat_id"),
        "mail": ("smtp_host", "mail_to"),
    }

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.channels = self._resolve_channels()

    def _resolve_channels(self) -> List[str]:
        if not self.cfg.notify_enabled:
            # 通知没打开时不要去校验渠道配置，否则会白刷一堆警告
            return []
        if not self.cfg.notify_channels:
            LOGGER.info("notify.enabled = true 但没有配置 channels，本次不会发送任何通知")
            return []
        active: List[str] = []
        for name in self.cfg.notify_channels:
            if name not in self.REQUIRED_FIELDS:
                LOGGER.warning("未知通知渠道，已忽略：%s", name)
                continue
            missing = [
                field for field in self.REQUIRED_FIELDS[name]
                if not str(getattr(self.cfg, field, "") or "").strip()
            ]
            if missing:
                LOGGER.warning("通知渠道 %s 缺少配置 %s，本次不会发送", name, " / ".join(missing))
                continue
            active.append(name)
        if active:
            LOGGER.info("已启用通知渠道：%s", ", ".join(active))
        else:
            LOGGER.warning("notify.enabled = true，但没有任何可用渠道，本次不会发送通知")
        return active

    @property
    def ready(self) -> bool:
        return bool(self.cfg.notify_enabled and self.channels)

    def enabled_for(self, already: bool, extra: bool = False) -> bool:
        """already=True 表示"今日已签到"这种重复触发，默认不打扰；

        extra=True 表示本次有值得知道的额外动作（比如补签），那就照发。
        """
        if not self.ready:
            return False
        if already and not (self.cfg.notify_always or extra):
            return False
        return True

    def send(self, title: str, content: str) -> List[str]:
        """发送到所有可用渠道，返回发送失败的渠道名列表（空列表 = 全部成功）。"""
        text = "%s\n%s" % (title, content)
        failed: List[str] = []
        for channel in self.channels:
            handler = getattr(self, "_send_" + channel, None)
            if handler is None:
                LOGGER.warning("未知通知渠道，已忽略：%s", channel)
                failed.append(channel)
                continue
            try:
                handler(title, text)
                LOGGER.info("通知已发送：%s", channel)
            except Exception as exc:  # 通知失败不影响签到结果
                LOGGER.warning("通知渠道 %s 发送失败：%s", channel, exc)
                failed.append(channel)
        return failed

    # -- 各渠道 ------------------------------------------------------------ #
    # 常见错误码 -> 人话（钉钉/企业微信的 errcode 含义不同，按渠道区分）
    ERROR_HINTS = {
        "dingtalk": {
            "300001": "access_token 无效：Webhook 地址复制不完整，或机器人已被删除",
            "310000": "安全设置未通过：机器人若用「自定义关键词」需填 T00ls；"
                      "若用「加签」需把 SEC 密钥填到 dingtalk_secret；"
                      "若用「IP 白名单」需加入本机出口 IP。"
                      "若以上都对，检查服务器时间是否准确（加签允许 ±1 小时偏差）："
                      "timedatectl status",
            "130101": "发送太快被钉钉限流，稍后再试",
        },
        "wecom": {
            "93000": "webhook key 无效，请重新复制企业微信机器人地址",
        },
    }

    def _send_dingtalk(self, title: str, text: str) -> None:
        if not self.cfg.dingtalk_webhook:
            raise ConfigError("未配置 dingtalk_webhook")
        webhook = self.cfg.dingtalk_webhook
        if self.cfg.dingtalk_secret:
            webhook = self.dingtalk_signed_url(webhook, self.cfg.dingtalk_secret)
        self._post_json(webhook, {"msgtype": "text", "text": {"content": text}}, channel="dingtalk")

    @staticmethod
    def dingtalk_signed_url(webhook: str, secret: str) -> str:
        """钉钉「加签」：在 Webhook 上追加 timestamp 与 HMAC-SHA256 签名。

        官方算法（与钉钉文档给出的 Python 示例一致）：
            stringToSign = "{timestamp}\\n{secret}"
            sign = urlencode(base64(HmacSHA256(stringToSign, secret)))   # key 是 secret
        timestamp 为毫秒级时间戳，服务端允许 ±1 小时偏差。
        """
        # 先把 webhook 里可能已经带的 timestamp/sign 去掉：很多人会把示例里拼好的
        # 完整 URL 直接粘过来，若再追加一次就会出现重复参数，导致验签失败。
        parts = urlsplit(webhook)
        kept = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in ("timestamp", "sign")
        ]
        query = urlencode(kept)
        base = urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

        timestamp = str(int(time.time() * 1000))
        string_to_sign = "%s\n%s" % (timestamp, secret)
        digest = hmac.new(
            secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
        joiner = "&" if query else "?"
        return "%s%stimestamp=%s&sign=%s" % (base, joiner, timestamp, sign)

    def _send_wecom(self, title: str, text: str) -> None:
        if not self.cfg.wecom_webhook:
            raise ConfigError("未配置 wecom_webhook")
        self._post_json(
            self.cfg.wecom_webhook, {"msgtype": "text", "text": {"content": text}}, channel="wecom"
        )

    def _send_serverchan(self, title: str, text: str) -> None:
        if not self.cfg.serverchan_key:
            raise ConfigError("未配置 serverchan_key")
        url = "https://sctapi.ftqq.com/%s.send" % self.cfg.serverchan_key
        response = self.session.post(url, data={"title": title, "desp": text}, timeout=15)
        self._check(response)

    def _send_bark(self, title: str, text: str) -> None:
        if not self.cfg.bark_url:
            raise ConfigError("未配置 bark_url")
        # 用 JSON POST 而不是把标题拼进 URL 路径：标题含空格和中文，走路径容易被
        # 转义或截断（bark_url 形如 https://api.day.app/你的key）
        response = self.session.post(
            self.cfg.bark_url.rstrip("/"),
            data=json.dumps({"title": title, "body": text}).encode("utf-8"),
            headers={"Content-Type": "application/json;charset=utf-8"},
            timeout=15,
        )
        self._check(response)

    def _send_telegram(self, title: str, text: str) -> None:
        if not (self.cfg.telegram_token and self.cfg.telegram_chat_id):
            raise ConfigError("未配置 telegram_token / telegram_chat_id")
        url = "https://api.telegram.org/bot%s/sendMessage" % self.cfg.telegram_token
        response = self.session.post(
            url, data={"chat_id": self.cfg.telegram_chat_id, "text": text}, timeout=15
        )
        self._check(response)

    def _send_mail(self, title: str, text: str) -> None:
        if not (self.cfg.smtp_host and self.cfg.mail_to):
            raise ConfigError("未配置 smtp_host / mail_to")
        message = MIMEText(text, "plain", "utf-8")
        message["From"] = formataddr(("T00ls 签到", self.cfg.mail_from or self.cfg.smtp_user))
        message["To"] = formataddr(("", self.cfg.mail_to))
        message["Subject"] = Header(title, "utf-8")
        if self.cfg.smtp_ssl:
            server = smtplib.SMTP_SSL(
                self.cfg.smtp_host, self.cfg.smtp_port, timeout=20,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=20)
            server.starttls(context=ssl.create_default_context())
        try:
            if self.cfg.smtp_user:
                server.login(self.cfg.smtp_user, self.cfg.smtp_password)
            server.sendmail(
                self.cfg.mail_from or self.cfg.smtp_user,
                [addr.strip() for addr in self.cfg.mail_to.split(",") if addr.strip()],
                message.as_string(),
            )
        finally:
            try:
                server.quit()
            except Exception:
                pass

    # -- 辅助 -------------------------------------------------------------- #
    def _post_json(self, url: str, payload: Dict[str, Any], channel: str = "") -> None:
        response = self.session.post(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json;charset=utf-8"},
            timeout=15,
        )
        self._check(response, channel=channel)

    @classmethod
    def _check(cls, response: requests.Response, channel: str = "") -> None:
        if response.status_code >= 400:
            raise T00lsError("HTTP %s：%s" % (response.status_code, response.text[:120]))
        body = (response.text or "").strip()
        if body.startswith("{") and '"errcode"' in body:
            data = json.loads(body)
            code = str(data.get("errcode", 0))
            if int(data.get("errcode", 0)) != 0:
                hint = cls.ERROR_HINTS.get(channel, {}).get(code, "")
                raise T00lsError(
                    "接口返回 errcode=%s errmsg=%s%s"
                    % (code, data.get("errmsg"), "（%s）" % hint if hint else "")
                )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="T00ls（www.t00ls.com）每日自动签到脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=QUESTION_HELP,
    )
    parser.add_argument("-c", "--config", help="配置文件路径（默认 ./config.ini 或 $T00LS_CONFIG）")
    parser.add_argument("--check", action="store_true", help="只查询今日签到状态，不提交签到")
    parser.add_argument("--dry-run", action="store_true", help="演练：登录并查询状态，但不提交签到")
    parser.add_argument(
        "--bu-sign", action="store_true",
        help="强制补签一次（不检查是否真的漏签，每次消耗 20 TuBi），用于手动核对接口行为",
    )
    parser.add_argument("--no-bu-sign", action="store_true", help="本次运行不做补签（覆盖配置里的 auto_bu_sign）")
    parser.add_argument("--no-notify", action="store_true", help="本次运行不发送任何通知")
    parser.add_argument(
        "--stale-retries", type=int, default=None,
        help="站点还没换日时的重试次数（默认取配置 stale_retries，即 3）",
    )
    parser.add_argument(
        "--stale-wait", type=float, default=None,
        help="每次重试前等待的秒数（默认取配置 stale_wait，即 120）",
    )
    parser.add_argument(
        "--test-notify", action="store_true",
        help="只发一条测试通知（不登录、不访问站点），用来验证钉钉等渠道是否配好",
    )
    parser.add_argument("--base-url", help="覆盖接口域名（默认 %s）" % DEFAULT_BASE_URL)
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version="t00ls_sign %s" % __version__)
    return parser.parse_args(argv)


def build_report(info: Dict[str, Any], username: str) -> List[str]:
    """状态摘要，既写日志也用于通知。

    时间统一用北京时间：签到是按论坛的"今天"结算的，服务器在 UTC 时如果显示本地时间，
    很容易让人误判是哪一天签的。
    """
    lines = [
        "账号：%s" % (username or "未知"),
        "时间：%s（北京时间）" % datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
    ]
    if info:
        lines.append("今日已签到：%s" % ("是" if T00lsClient.is_signed_today(info) else "否"))
        if info.get("sign_times") is not None:
            lines.append("累计签到：%s 次" % info.get("sign_times"))
        if info.get("extcredits2") is not None:
            lines.append("TuBi：%s" % info.get("extcredits2"))
        if info.get("extcredits1") is not None:
            lines.append("TCV：%s" % info.get("extcredits1"))
    return lines


def log_report(lines: List[str]) -> None:
    for line in lines:
        LOGGER.info("    %s", line)


def try_bu_sign(
    client: T00lsClient,
    cfg: Config,
    state: State,
    today: str,
    info: Dict[str, Any],
    force: bool = False,
) -> Optional[str]:
    """按需补签，返回一行结果说明（用于日志和通知）；None 表示没有补签。

    安全性来自三道闸门：
      1) 只有检测到真的漏签才补（force 例外），检测用站点自己的累计签到次数；
      2) 每天最多补一次——定时器一天会跑 3 次，没有这个记录就会重复扣 20 TuBi；
      3) 记录「先落盘再请求」，即便请求中途异常也不会同一天重试。
    """
    if state.bu_sign_done_today(today) and not force:
        LOGGER.info(
            "今日已经尝试过补签，跳过（避免重复消耗 TuBi）；确需重试请手动执行 --bu-sign"
        )
        return None

    sign_times = as_int(info.get("sign_times"))
    if force:
        LOGGER.warning("手动强制补签：不检查是否真的漏签，每次消耗 20 TuBi")
    else:
        missed, detail = state.detect_missed_days(today, sign_times)
        if missed is None:
            LOGGER.info("本次不补签：%s（正常跑满一天后即可自动判断漏签）", detail)
            return None
        if missed < 1:
            LOGGER.info("未检测到漏签，无需补签（%s）", detail)
            return None
        if missed > cfg.bu_sign_within_days:
            LOGGER.warning(
                "检测到漏签 %d 天，超过 bu_sign_within_days=%d，跳过补签"
                "（久远的日期多半不允许补，确实需要请手动加 --bu-sign）",
                missed, cfg.bu_sign_within_days,
            )
            return None
        LOGGER.info("检测到漏签 %d 天（%s），尝试补签（每次消耗 20 TuBi）", missed, detail)

    state.mark_bu_sign(today)
    state.save()

    tubi_before = as_int(info.get("extcredits2"))
    try:
        result = client.bu_sign()
    except T00lsError as exc:
        # 补签失败不能牵连签到结论：签到本身已经成功，这里只报告补签没成，
        # 也不要在同一天重试（宁可少补一次，也不冒重复扣 20 TuBi 的风险）。
        line = "补签：失败 - %s（今日不再重试，必要时手动 --bu-sign）" % exc
        LOGGER.warning("%s", line)
        return line
    # 该接口返回值语义官方文档没写全，把原始返回打出来，方便用自己的账号核对
    LOGGER.info("补签接口原始返回：%s", json.dumps(result.raw, ensure_ascii=False))

    time.sleep(1.5)
    try:
        refreshed = client.profile()
    except T00lsError as exc:
        LOGGER.warning("补签后复核失败：%s", exc)
        refreshed = {}
    if refreshed:
        info.update(refreshed)
    tubi_after = as_int(refreshed.get("extcredits2"))
    if tubi_before is not None and tubi_after is not None:
        cost = "，TuBi %s -> %s" % (tubi_before, tubi_after)
    else:
        cost = ""

    if result.ok and not result.already:
        line = "补签：已提交（%s%s）" % (result.message, cost)
        LOGGER.info("%s", line)
        return line
    line = "补签：未成功 - %s%s" % (result.message, cost)
    LOGGER.warning("%s", line)
    return line


def run_test_notify(cfg: Config) -> int:
    """只验证通知渠道，不登录也不签到。"""
    notifier = Notifier(cfg)
    if not notifier.ready:
        LOGGER.error(
            "没有可用的通知渠道，无法测试。请检查 config.ini 的 [notify]："
            "enabled = true、channels 包含 dingtalk、并且 dingtalk_webhook 已填"
            "（当前 enabled=%s，channels=%s）",
            cfg.notify_enabled, ",".join(cfg.notify_channels) or "(空)",
        )
        return 2

    body = "\n".join([
        "这是一条测试通知：脚本没有登录、也没有签到。",
        "时间：%s（北京时间）" % datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "渠道：%s" % ", ".join(notifier.channels),
        "收到这条消息说明通知配置正常；日常只在签到成功/失败/补签时才推送。",
    ])
    LOGGER.info("即将发送的测试消息内容：")
    for line in body.splitlines():
        LOGGER.info("    %s", line)
    failed = notifier.send("T00ls 签到：测试通知", body)
    if failed:
        LOGGER.error("测试通知发送失败：%s（具体原因见上面的警告行）", ", ".join(failed))
        return 1
    LOGGER.info("测试通知已发出：%s —— 请到钉钉群确认收到了这条消息", ", ".join(notifier.channels))
    return 0


def run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        # 此时还没有日志配置，直接写 stderr，保持退出码语义清晰（2 = 配置错误）
        sys.stderr.write("配置错误：%s\n" % exc)
        return 2
    if args.base_url:
        cfg.base_url = args.base_url
    if args.no_bu_sign:
        cfg.bu_sign = False
    setup_logging(args.verbose, cfg.log_file)

    # 只测通知时不需要账号，所以放在登录配置校验之前
    if args.test_notify:
        return run_test_notify(cfg)

    if not cfg.has_login:
        LOGGER.error(
            "未配置登录信息：请在 config.ini 的 [t00ls] 中填写 username + password"
            "（或 password_md5 / cookie）"
        )
        return 2

    client = T00lsClient(cfg)
    notifier = Notifier(cfg)
    state = State(cfg.state_file)
    state.load()
    today = today_cst()
    # 把版本和"正在跑的是哪个文件"打进日志：升级后怀疑没生效时，journalctl 一眼可查
    LOGGER.info("t00ls_sign v%s 启动（脚本：%s）", __version__, os.path.abspath(__file__))
    LOGGER.info("签到日期（北京时间）：%s", today)
    info: Dict[str, Any] = {}
    bu_line: Optional[str] = None
    try:
        client.login()
        info = client.profile()

        if T00lsClient.is_signed_today(info):
            if (
                not args.check
                and not args.dry_run
                and looks_like_stale_sign_today(state, info, today)
            ):
                info = resolve_stale_sign_today(
                    client,
                    today,
                    info,
                    retries=args.stale_retries if args.stale_retries is not None else cfg.stale_retries,
                    wait=args.stale_wait if args.stale_wait is not None else cfg.stale_wait,
                )
                action = "签到成功"
                already = False
            else:
                action = "今日已签到，无需重复签到"
                already = True
        elif args.check:
            action = "今日尚未签到（--check 模式，未提交）"
            already = False
        elif args.dry_run:
            action = "演练模式：跳过签到提交（--dry-run）"
            already = False
        else:
            result = client.sign()
            already = result.already
            action = result.message
            if result.already:
                LOGGER.info("今日已签到（接口返回 alreadysign）")
            else:
                LOGGER.info("签到成功，正在复核…")
            time.sleep(1.5)
            try:
                refreshed = client.profile()
                if refreshed:
                    info = refreshed
            except T00lsError as exc:
                LOGGER.warning("签到后复核失败（不影响结论）：%s", exc)
            if not already and not T00lsClient.is_signed_today(info):
                # 接口说成功但状态没变，保守地按失败处理，交给定时任务下次重试
                raise T00lsError("接口返回成功，但 sign_today 仍为 0，请手动确认")
            if not already:
                action = "签到成功"

        # 补签：仅在自己的签到已经确认的前提下判断，且 --check / --dry-run 不动钱
        want_bu_sign = (cfg.bu_sign or args.bu_sign) and not args.check and not args.dry_run
        if want_bu_sign and T00lsClient.is_signed_today(info):
            bu_line = try_bu_sign(
                client, cfg, state, today, info, force=bool(args.bu_sign)
            )

        # 只有"确认今天已签到"才更新基线，保证漏签判断的日期与计数是同一时刻的
        if T00lsClient.is_signed_today(info) and not args.dry_run:
            state.record_signed(today, as_int(info.get("sign_times")), as_int(info.get("extcredits2")))
            state.save()

        report_lines = build_report(info, client.username or cfg.username)
        if bu_line:
            report_lines.append(bu_line)
        LOGGER.info("完成：%s", action)
        log_report(report_lines)
        if not args.no_notify and notifier.enabled_for(already, extra=bool(bu_line)):
            notifier.send("T00ls 签到：%s" % action, "结果：%s\n%s" % (action, "\n".join(report_lines)))
        return 0

    except ConfigError as exc:
        LOGGER.error("配置错误：%s", exc)
        return 2
    except T00lsError as exc:
        LOGGER.error("失败：%s", exc)
        report_lines = build_report(info, client.username or cfg.username)
        log_report(report_lines)
        if not args.no_notify and notifier.ready:
            notifier.send("T00ls 签到失败", "结果：失败 - %s\n%s" % (exc, "\n".join(report_lines)))
        return 1
    finally:
        client.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        LOGGER.warning("已被用户中断")
        return 1


if __name__ == "__main__":
    sys.exit(main())
