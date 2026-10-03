# -*- coding: utf-8 -*-
"""本地自检：用 mock HTTP 服务模拟 T00ls 官方接口，端到端跑各种场景。"""
import base64
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote_plus, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import t00ls_sign  # noqa: E402

USERNAME = "tester"
PASSWORD = "Passw0rd!"
PASSWORD_MD5 = hashlib.md5(PASSWORD.encode()).hexdigest()
QUESTION_ID = 5
QUESTION_ANSWER = "ThinkPad"

STATE = {
    "password_md5": PASSWORD_MD5,
    "question_id": QUESTION_ID,
    "question_answer": QUESTION_ANSWER,
    "signed_today": False,
    "sign_status": "success",          # success / alreadysign / wrongsubmit_once / success_but_not_signed
    "sign_times": "41",
    "tubi": "120",
    "counts": {},
    "wrongsubmit_used": False,
    # 补签接口行为（官方文档没写全，两种可能都测）
    "busign_status": "success",        # success / wrongbusubmit / fail
    "busign_costs_tubi": True,
    "busign_bumps_sign_times": True,
}

TMP_DIR = tempfile.mkdtemp(prefix="t00ls-selftest-")
STATE_FILE = os.path.join(TMP_DIR, "t00ls-state.json")

# 通知渠道的请求会被记录到这里，用来校验真实发出的 payload 形状
CAPTURED = []
# 301 跳转服务器指向的真实 mock 地址
REDIRECT_TARGET = {"base": "", "chain": ""}


def jdate(days_ago=0):
    """相对北京时间的日期字符串。"""
    day = datetime.now(timezone(timedelta(hours=8))).date() - timedelta(days=days_ago)
    return day.strftime("%Y-%m-%d")


def write_state(last_sign_date=None, last_sign_times=None, bu_sign_dates=None):
    data = {"version": 1}
    if last_sign_date:
        data["last_sign_date"] = last_sign_date
    if last_sign_times is not None:
        data["last_sign_times"] = last_sign_times
    if bu_sign_dates:
        data["bu_sign_dates"] = bu_sign_dates
    with open(STATE_FILE, "w", encoding="utf-8") as fp:
        json.dump(data, fp)


def bump(key):
    STATE["counts"][key] = STATE["counts"].get(key, 0) + 1


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, obj, code=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        cookie = self.headers.get("Cookie", "")
        return "_auth=ok" in cookie

    def _form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        data = {}
        for pair in raw.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                data[k] = v
        return data

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/members-profile.json":
            bump("profile")
            if not self._authed():
                return self._send({"status": "fail", "message": "loginfirst"})
            signed = STATE["signed_today"] or STATE["sign_status"] == "already_in_profile"
            payload = {
                "status": "success",
                "memberinfo": {
                    "uid": "12345",
                    "username": USERNAME,
                    "sign_times": STATE["sign_times"],
                    "sign_today": "1" if signed else "0",
                    "extcredits1": "7",
                    "extcredits2": STATE["tubi"],
                    "formhash": "a1b2c3d4",
                },
            }
            self._send(payload)
            # 模拟"站点在读完这次状态之后才换日"：即刚过北京 0 点时的那种竞态
            if STATE.get("rollover_after_profiles"):
                STATE["profile_seen"] = STATE.get("profile_seen", 0) + 1
                if STATE["profile_seen"] >= STATE["rollover_after_profiles"]:
                    STATE["signed_today"] = False
                    STATE["rollover_after_profiles"] = 0
            return
        self._send({"status": "fail", "message": "notfound"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/login.json":
            bump("login")
            if STATE.get("login_fail_message") is not None:
                return self._send({"status": "fail", "message": STATE["login_fail_message"]})
            data = self._form()
            ok = (
                data.get("action") == "login"
                and data.get("username") == USERNAME
                and data.get("password") == STATE["password_md5"]
                and data.get("questionid") == str(STATE["question_id"])
                and data.get("answer") == STATE["question_answer"]
            )
            if not ok:
                return self._send({"status": "fail", "message": "login_question_invalid"})
            return self._send(
                {"status": "success", "formhash": "a1b2c3d4", "memberinfo": "login_succeed"},
                extra_headers={"Set-Cookie": "_auth=ok; Path=/"},
            )

        if path == "/ajax-sign.json":
            bump("sign")
            if not self._authed():
                return self._send({"status": "fail", "message": "loginfirst"})
            data = self._form()
            if data.get("signsubmit") != "true":
                return self._send({"status": "fail", "message": "wrongsubmit"})
            mode = STATE["sign_status"]
            if mode == "alreadysign":
                return self._send({"status": "success", "memberinfo": "alreadysign"})
            if mode == "wrongsubmit_once" and not STATE["wrongsubmit_used"]:
                STATE["wrongsubmit_used"] = True
                return self._send({"status": "fail", "message": "wrongsubmit"})
            if mode == "success_but_not_signed":
                return self._send({"status": "success", "message": "sign_success"})
            STATE["signed_today"] = True
            # 真实站点签到成功后累计签到次数会 +1，mock 也要跟着动，否则漏签判断无从验证
            STATE["sign_times"] = str(int(STATE["sign_times"]) + 1)
            return self._send({"status": "success", "message": "sign_success"})

        if path == "/ajax-busign.json":
            bump("busign")
            if not self._authed():
                return self._send({"status": "fail", "message": "loginfirst"})
            data = self._form()
            if data.get("signsubmit") != "true":
                return self._send({"status": "success", "memberinfo": "wrongbusubmit"})
            mode = STATE["busign_status"]
            if mode == "wrongbusubmit":
                return self._send({"status": "success", "memberinfo": "wrongbusubmit"})
            if mode == "fail":
                return self._send({"status": "fail", "message": "nosignday"})
            if mode == "http500":
                return self._send({"status": "fail", "message": "boom"}, code=500)
            if STATE["busign_costs_tubi"]:
                STATE["tubi"] = str(int(STATE["tubi"]) - 20)
            if STATE["busign_bumps_sign_times"]:
                STATE["sign_times"] = str(int(STATE["sign_times"]) + 1)
            return self._send({"status": "success", "message": "sign_success"})

        # ---- 记录型端点：校验通知渠道真实发出的 payload ----
        if path in ("/dingtalk-hook", "/wecom-hook", "/bark/KEY", "/dingtalk-error"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            # 存完整 self.path（含 query），这样能校验加签参数
            CAPTURED.append((self.path, raw))
            if path == "/bark/KEY":
                return self._send({"code": 200, "message": "success"})
            if path == "/dingtalk-error":
                return self._send({"errcode": 310000, "errmsg": "keywords not in content"})
            return self._send({"errcode": 0, "errmsg": "ok"})

        self._send({"status": "fail", "message": "notfound"}, 404)


class RedirectHandler(BaseHTTPRequestHandler):
    """模拟 www.t00ls.net -> www.t00ls.com 的 301 跨站跳转（换端口即换 origin）。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _redirect(self):
        bump("redirect")
        self.send_response(301)
        self.send_header("Location", REDIRECT_TARGET["base"] + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _redirect
    do_POST = _redirect


class ChainRedirectHandler(RedirectHandler):
    """两跳跳转：A -> B -> 真实 mock，用来验证跳转链不会被跟丢。"""

    def _redirect(self):
        bump("redirect")
        self.send_response(301)
        self.send_header("Location", REDIRECT_TARGET["chain"] + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # 必须重新绑定：父类里 do_GET/do_POST 指向的是父类的 _redirect 函数对象，
    # 不重新赋值的话子类会直接跳到终点，跳转链就形同虚设（这个坑测试里踩过一次）。
    do_GET = _redirect
    do_POST = _redirect


def reset(**kwargs):
    global STATE
    STATE.update({
        "signed_today": False,
        "sign_status": "success",
        "sign_times": "41",
        "tubi": "120",
        "counts": {},
        "wrongsubmit_used": False,
        "login_fail_message": None,
        "busign_status": "success",
        "busign_costs_tubi": True,
        "busign_bumps_sign_times": True,
        "rollover_after_profiles": 0,
        "profile_seen": 0,
    })
    STATE.update(kwargs)
    logging.root.handlers.clear()
    del CAPTURED[:]
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


def write_config(path, **kw):
    body = kw.get("body")
    if body is None:
        body = (
            "[t00ls]\n"
            "base_url = {base}\n"
            "username = {username}\n"
            "password = {password}\n"
            "question_id = {qid}\n"
            "question_answer = {qans}\n"
            "cookie = {cookie}\n"
            "timeout = 5\n"
            "retries = 2\n"
            "log_file =\n"
            "state_file = {state}\n"
            "auto_bu_sign = {bu}\n"
            "bu_sign_within_days = {within}\n"
            "[notify]\nenabled = {notify_on}\nchannels = {channels}\n"
            "dingtalk_webhook = {hook}\n"
        ).format(
            base=kw.get("base", ""),
            username=kw.get("username", USERNAME),
            password=kw.get("password", PASSWORD),
            qid=kw.get("qid", QUESTION_ID),
            qans=kw.get("qans", QUESTION_ANSWER),
            cookie=kw.get("cookie", ""),
            state=kw.get("state", STATE_FILE),
            bu="true" if kw.get("bu") else "false",
            within=kw.get("within", 3),
            notify_on="true" if kw.get("notify") else "false",
            channels=kw.get("channels", ""),
            hook=kw.get("hook", ""),
        )
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(body)


def run_case(label, argv, expect_rc, expect=None, forbid=None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = t00ls_sign.main(argv)
    text = out.getvalue()
    problems = []
    if rc != expect_rc:
        problems.append("退出码 %s != 期望 %s" % (rc, expect_rc))
    for needle in (expect or []):
        if needle not in text:
            problems.append("日志缺少 %r" % needle)
    for needle in (forbid or []):
        if needle in text:
            problems.append("出现了不该有的 %r" % needle)
    status = "PASS" if not problems else "FAIL"
    print("[%s] %s (rc=%s)" % (status, label, rc))
    if problems:
        print("       " + "; ".join(problems))
        print("       ---- 输出 ----")
        for line in text.strip().splitlines():
            print("       " + line)
    return not problems


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port

    # 跨站 301 跳转服务器（真实复现 www.t00ls.net -> www.t00ls.com 的行为）
    redirect_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_base = "http://127.0.0.1:%d" % redirect_server.server_address[1]
    REDIRECT_TARGET["base"] = base
    threading.Thread(target=redirect_server.serve_forever, daemon=True).start()

    # 两跳跳转：chain_base -> redirect_base -> base
    chain_server = ThreadingHTTPServer(("127.0.0.1", 0), ChainRedirectHandler)
    chain_base = "http://127.0.0.1:%d" % chain_server.server_address[1]
    REDIRECT_TARGET["chain"] = redirect_base
    threading.Thread(target=chain_server.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="t00ls-selftest-")
    cfg = os.path.join(tmp, "config.ini")

    results = []

    # 1. 正常签到
    reset()
    write_config(cfg)
    results.append(run_case(
        "正常签到成功", ["-c", cfg, "--base-url", base], 0,
        expect=["登录成功", "签到成功", "今日已签到：是", "TuBi：120", "（北京时间）"],
    ))
    results.append(STATE["counts"].get("sign") == 1 or print("       (sign 提交次数异常)"))
    print("       提交统计:", STATE["counts"])

    # 2. 已签到（sign_today=1）：不应再调签到接口
    reset(signed_today=True)
    write_config(cfg)
    results.append(run_case(
        "已签到则跳过", ["-c", cfg, "--base-url", base], 0,
        expect=["今日已签到，无需重复签到"],
    ))
    print("       提交统计:", STATE["counts"], "(sign 应为 0)")
    results.append(STATE["counts"].get("sign", 0) == 0)

    # 3. --check 不提交签到
    reset()
    write_config(cfg)
    results.append(run_case(
        "--check 只查询", ["-c", cfg, "--base-url", base, "--check"], 0,
        expect=["今日尚未签到"],
    ))
    results.append(STATE["counts"].get("sign", 0) == 0)

    # 4. --dry-run 不提交签到
    reset()
    write_config(cfg)
    results.append(run_case(
        "--dry-run 演练", ["-c", cfg, "--base-url", base, "--dry-run"], 0,
        expect=["演练模式"],
    ))
    results.append(STATE["counts"].get("sign", 0) == 0)

    # 5. 密码/安全提问错误
    reset()
    write_config(cfg, qid=3, qans="wrong")
    results.append(run_case(
        "安全提问错误", ["-c", cfg, "--base-url", base], 1,
        expect=["登录失败", "login_question_invalid"],
    ))

    # 5b. 真实接口的数字错误码 / 限流提示
    for raw, needle in (
        (1, "密码为空"),
        (2, "用户名为空"),
        (3, "用户名或密码不对"),
        (4, "安全提问不正确"),
        ("5failedlogin,plswait15min", "限制登录"),
        (9, "登录失败：9"),
    ):
        reset(login_fail_message=raw)
        write_config(cfg)
        results.append(run_case(
            "登录错误码 %r" % (raw,), ["-c", cfg, "--base-url", base], 1,
            expect=[needle], forbid=["Traceback"],
        ))

    # 6. formhash 失效自动重试
    reset(sign_status="wrongsubmit_once")
    write_config(cfg)
    results.append(run_case(
        "wrongsubmit 自动重试", ["-c", cfg, "--base-url", base], 0,
        expect=["formhash 失效", "签到成功"],
    ))
    print("       提交统计:", STATE["counts"], "(sign 应为 2)")
    results.append(STATE["counts"].get("sign") == 2)

    # 7. 接口回 alreadysign
    reset(sign_status="alreadysign")
    write_config(cfg)
    results.append(run_case(
        "接口返回 alreadysign", ["-c", cfg, "--base-url", base], 0,
        expect=["alreadysign"],
    ))

    # 8. 接口说成功但状态没变 -> 视为失败
    reset(sign_status="success_but_not_signed")
    write_config(cfg)
    results.append(run_case(
        "成功但未生效按失败处理", ["-c", cfg, "--base-url", base], 1,
        expect=["sign_today 仍为 0"],
    ))

    # 9. Cookie 模式
    reset()
    write_config(cfg, username="", password="", cookie="_auth=ok; _saltkey=abc")
    results.append(run_case(
        "Cookie 模式", ["-c", cfg, "--base-url", base], 0,
        expect=["Cookie 有效", "签到成功"],
    ))
    print("       login 提交次数(应为 0):", STATE["counts"].get("login", 0))

    # 10. Cookie 失效
    reset()
    write_config(cfg, username="", password="", cookie="_auth=bad")
    results.append(run_case(
        "Cookie 失效", ["-c", cfg, "--base-url", base], 1,
        expect=["Cookie 已失效"],
    ))

    # 11. 未配置任何凭据
    reset()
    write_config(cfg, username="", password="", cookie="")
    results.append(run_case(
        "未配置凭据", ["-c", cfg, "--base-url", base], 2,
        expect=["未配置登录信息"],
    ))

    # 12. 缺少配置文件
    reset()
    results.append(run_case(
        "配置文件不存在", ["-c", os.path.join(tmp, "nope.ini")], 2,
        expect=["配置文件不存在"],
    ))

    # 12b. 带 UTF-8 BOM 的配置（Windows 记事本/Set-Content 的常见产物）
    reset()
    bom_cfg = os.path.join(tmp, "bom.ini")
    with open(bom_cfg, "w", encoding="utf-8-sig") as fp:
        fp.write("[t00ls]\nusername = %s\npassword = %s\nquestion_id = %s\n"
                 "question_answer = %s\nlog_file =\n" % (USERNAME, PASSWORD, QUESTION_ID, QUESTION_ANSWER))
    results.append(run_case(
        "带 BOM 的配置文件", ["-c", bom_cfg, "--base-url", base, "--check"], 0,
        expect=["今日尚未签到"],
    ))

    # 12c. 内容损坏的配置文件 -> rc=2 且不抛 traceback
    reset()
    bad_cfg = os.path.join(tmp, "bad.ini")
    with open(bad_cfg, "w", encoding="utf-8") as fp:
        fp.write("this is not an ini file\n")
    results.append(run_case(
        "损坏的配置文件", ["-c", bad_cfg], 2,
        expect=["配置文件格式错误"],
        forbid=["Traceback"],
    ))

    # 13. 通知开关：已签到时默认不通知
    reset(signed_today=True)
    notify_body = (
        "[t00ls]\nusername = %s\npassword = %s\nquestion_id = %s\nquestion_answer = %s\n"
        "log_file =\nstate_file = %s\n"
        "[notify]\nenabled = true\nchannels = dingtalk\n"
        "dingtalk_webhook = https://example.invalid/robot/send?access_token=fake\n"
    ) % (USERNAME, PASSWORD, QUESTION_ID, QUESTION_ANSWER, STATE_FILE)
    write_config(cfg, body=notify_body)
    notified = []
    orig_send = t00ls_sign.Notifier.send
    t00ls_sign.Notifier.send = lambda self, t, c: notified.append((t, c))
    try:
        results.append(run_case(
            "已签到默认不通知", ["-c", cfg, "--base-url", base], 0,
            expect=["今日已签到"],
        ))
        results.append(not notified)
        print("       通知次数(应为 0):", len(notified))
        reset()
        write_config(cfg, body=notify_body)
        results.append(run_case(
            "签到成功会通知", ["-c", cfg, "--base-url", base], 0,
            expect=["签到成功"],
        ))
        print("       通知次数(应为 1):", len(notified))
        results.append(len(notified) == 1 and "T00ls 签到" in notified[0][0])
        print("       通知标题:", notified[0][0] if notified else "-")
        print("       通知正文:", (notified[0][1].replace("\n", " | ") if notified else "-"))
    finally:
        t00ls_sign.Notifier.send = orig_send

    # 14. 无效 cookie 字符串归一化
    print("[INFO] normalize_cookie:", t00ls_sign.normalize_cookie("Cookie: a=1;\nb=2;\na=1"))

    # 16. 自动补签
    print("\n--- 自动补签（每次消耗 20 TuBi，重点是别重复扣费） ---")
    send_stub = []

    def _stub_send(self, title, content):
        send_stub.append((title, content))

    orig_send = t00ls_sign.Notifier.send
    t00ls_sign.Notifier.send = _stub_send
    try:
        # 16a. 默认关闭
        reset()
        write_state(last_sign_date=jdate(2), last_sign_times=41)
        write_config(cfg)
        results.append(run_case(
            "16a 默认不补签", ["-c", cfg, "--base-url", base], 0,
            expect=["签到成功"], forbid=["补签接口原始返回"],
        ))
        print("       busign 次数(应为 0):", STATE["counts"].get("busign", 0))
        results.append(STATE["counts"].get("busign", 0) == 0)

        # 16b. 开启补签但首次运行没有基线
        reset()
        write_config(cfg, bu=True)
        results.append(run_case(
            "16b 首次运行无基线不补签", ["-c", cfg, "--base-url", base], 0,
            expect=["没有基线"],
        ))
        results.append(STATE["counts"].get("busign", 0) == 0)

        # 16c. 有基线且没漏签
        reset()
        write_state(last_sign_date=jdate(1), last_sign_times=41)
        write_config(cfg, bu=True)
        results.append(run_case(
            "16c 没漏签不补签", ["-c", cfg, "--base-url", base], 0,
            expect=["未检测到漏签"],
        ))
        results.append(STATE["counts"].get("busign", 0) == 0)

        # 16d. 漏签 1 天 -> 补签 1 次 + 通知带补签结果
        reset()
        write_state(last_sign_date=jdate(2), last_sign_times=41)
        write_config(cfg, bu=True, notify=True, channels="dingtalk",
                     hook="https://example.invalid/robot/send?access_token=fake")
        send_stub.clear()
        results.append(run_case(
            "16d 漏签则补签", ["-c", cfg, "--base-url", base], 0,
            expect=["检测到漏签 1 天", "补签接口原始返回", "补签：已提交", "TuBi 120 -> 100"],
        ))
        print("       busign 次数(应为 1):", STATE["counts"].get("busign", 0))
        results.append(STATE["counts"].get("busign", 0) == 1)
        print("       通知:", len(send_stub), "|", send_stub[0][0] if send_stub else "-")
        results.append(len(send_stub) == 1 and "补签" in send_stub[0][1])

        # 16e. 同一天第二次运行（定时器一天跑 3 次）不能重复补签
        STATE["signed_today"] = True
        results.append(run_case(
            "16e 同一天不重复补签", ["-c", cfg, "--base-url", base], 0,
            expect=["已经尝试过补签"],
        ))
        print("       busign 次数(应仍为 1):", STATE["counts"].get("busign", 0),
              "| 通知数(应仍为 1):", len(send_stub))
        results.append(STATE["counts"].get("busign", 0) == 1 and len(send_stub) == 1)

        # 16f. 补签被拒（wrongbusubmit）不应崩，也不要重试
        reset(busign_status="wrongbusubmit")
        write_state(last_sign_date=jdate(2), last_sign_times=41)
        write_config(cfg, bu=True)
        results.append(run_case(
            "16f 补签被拒", ["-c", cfg, "--base-url", base], 0,
            expect=["补签：未成功", "wrongbusubmit"], forbid=["Traceback"],
        ))
        results.append(STATE["counts"].get("busign", 0) == 1)

        # 16g. 漏签太久（超过 bu_sign_within_days）
        reset()
        write_state(last_sign_date=jdate(10), last_sign_times=41)
        write_config(cfg, bu=True, within=3)
        results.append(run_case(
            "16g 漏签太久不补", ["-c", cfg, "--base-url", base], 0,
            expect=["超过 bu_sign_within_days=3"],
        ))
        results.append(STATE["counts"].get("busign", 0) == 0)

        # 16h. --bu-sign 强制补签（不检查漏签）
        reset(signed_today=True)
        write_state(last_sign_date=jdate(1), last_sign_times=41)
        write_config(cfg)
        results.append(run_case(
            "16h --bu-sign 强制补签", ["-c", cfg, "--base-url", base, "--bu-sign"], 0,
            expect=["手动强制补签", "补签：已提交"],
        ))
        results.append(STATE["counts"].get("busign", 0) == 1)

        # 16i. --no-bu-sign 覆盖配置
        reset(signed_today=True)
        write_state(last_sign_date=jdate(2), last_sign_times=41)
        write_config(cfg, bu=True)
        results.append(run_case(
            "16i --no-bu-sign 覆盖配置", ["-c", cfg, "--base-url", base, "--no-bu-sign"], 0,
            expect=["今日已签到"], forbid=["补签接口原始返回"],
        ))
        results.append(STATE["counts"].get("busign", 0) == 0)

        # 16j. --check 只读，不该动钱
        reset()
        write_state(last_sign_date=jdate(2), last_sign_times=41)
        write_config(cfg, bu=True)
        results.append(run_case(
            "16j --check 不补签", ["-c", cfg, "--base-url", base, "--check"], 0,
            expect=["今日尚未签到"],
        ))
        results.append(STATE["counts"].get("busign", 0) == 0)

        # 16k. 今日已签到（比如用 App 签的），但昨天漏了 -> 也应该补
        reset(signed_today=True)
        write_state(last_sign_date=jdate(2), last_sign_times=41)
        write_config(cfg, bu=True)
        results.append(run_case(
            "16k 今日已签仍补昨天", ["-c", cfg, "--base-url", base], 0,
            expect=["检测到漏签 1 天", "补签：已提交"],
        ))
        results.append(STATE["counts"].get("busign", 0) == 1)

        # 16l. 通知渠道缺少 webhook 时启动就明确跳过
        reset(signed_today=True)
        write_config(cfg, notify=True, channels="dingtalk")
        results.append(run_case(
            "16l 渠道未配置则跳过", ["-c", cfg, "--base-url", base], 0,
            expect=["缺少配置 dingtalk_webhook"],
        ))
    finally:
        t00ls_sign.Notifier.send = orig_send

    # 17. 本轮复查发现的缺陷 -> 回归测试
    print("\n--- 复查发现的缺陷（回归测试） ---")

    # 17a. 补签请求失败（HTTP 500）不应把整轮判成“签到失败”
    reset(busign_status="http500")
    write_state(last_sign_date=jdate(2), last_sign_times=41)
    write_config(cfg, bu=True, notify=True, channels="dingtalk",
                 hook="http://127.0.0.1:%d/dingtalk-hook" % port)
    send_stub = []
    orig_send = t00ls_sign.Notifier.send
    t00ls_sign.Notifier.send = lambda self, t, c: send_stub.append((t, c))
    try:
        results.append(run_case(
            "17a 补签失败不影响签到结论", ["-c", cfg, "--base-url", base], 0,
            expect=["完成：签到成功", "补签：失败", "连续 2 次失败"],
            forbid=["T00ls 签到失败", "Traceback"],
        ))
        titles = [t for t, _ in send_stub]
        print("       通知标题:", titles)
        results.append(titles == ["T00ls 签到：签到成功"])
        # 17b. 补签发过请求但失败了，同一天也不能再试一次（状态已先落盘）
        before = STATE["counts"].get("busign", 0)
        STATE["signed_today"] = True
        logging.root.handlers.clear()
        results.append(run_case(
            "17b 补签失败后当天不再重试", ["-c", cfg, "--base-url", base, "--no-notify"], 0,
            expect=["已经尝试过补签"],
        ))
        print("       busign 次数(应仍为 %d): %s" % (before, STATE["counts"].get("busign", 0)))
        results.append(STATE["counts"].get("busign", 0) == before)
    finally:
        t00ls_sign.Notifier.send = orig_send

    # 17c. 跨站 301 跳转（.net -> .com）：应自动切换域名并重发 POST，而不是报 wrongaction
    reset()
    write_config(cfg)
    results.append(run_case(
        "17c 跨域 301 自动切换域名", ["-c", cfg, "--base-url", redirect_base], 0,
        expect=["跳转到了", "已自动改用新域名重发请求", "登录成功", "签到成功"],
        forbid=["wrongaction", "Traceback"],
    ))
    print("       跳转次数:", STATE["counts"].get("redirect", 0),
          "| login 次数:", STATE["counts"].get("login", 0))

    # 17c2. 连续两跳跳转也要跟到底
    reset()
    write_config(cfg)
    results.append(run_case(
        "17c2 两跳跳转链", ["-c", cfg, "--base-url", chain_base], 0,
        expect=["跳转到了", "登录成功", "签到成功"],
        forbid=["wrongaction", "Traceback"],
    ))
    print("       跳转次数:", STATE["counts"].get("redirect", 0))

    # 17d. 通知关闭时不该因为缺 webhook 刷警告
    reset(signed_today=True)
    write_config(cfg, channels="dingtalk")      # enabled = false
    results.append(run_case(
        "17d 通知关闭时不校验渠道", ["-c", cfg, "--base-url", base], 0,
        expect=["今日已签到"],
        forbid=["缺少配置 dingtalk_webhook"],
    ))

    # 17e. notify.enabled=true 但没写 channels：要给出明确提示
    reset(signed_today=True)
    write_config(cfg, notify=True)
    results.append(run_case(
        "17e 开了通知却没写 channels", ["-c", cfg, "--base-url", base], 0,
        expect=["没有配置 channels"],
    ))

    # 17f. 环境变量 T00LS_PASSWORD 必须压过配置文件里残留的 password_md5
    reset()
    stale_body = (
        "[t00ls]\nusername = %s\npassword_md5 = %s\nquestion_id = %s\nquestion_answer = %s\n"
        "log_file =\nstate_file = %s\n"
    ) % (USERNAME, hashlib.md5(b"totally-wrong").hexdigest(), QUESTION_ID, QUESTION_ANSWER, STATE_FILE)
    write_config(cfg, body=stale_body)
    os.environ["T00LS_PASSWORD"] = PASSWORD
    try:
        results.append(run_case(
            "17f 环境变量密码优先", ["-c", cfg, "--base-url", base], 0,
            expect=["登录成功", "签到成功"],
        ))
    finally:
        os.environ.pop("T00LS_PASSWORD", None)

    # 17g. 钉钉 payload 形状（真实发一次到 mock，校验关键词与结构）
    reset(signed_today=True)
    hook_cfg = os.path.join(tmp, "hook.ini")
    write_config(hook_cfg, notify=True, channels="dingtalk",
                 hook="http://127.0.0.1:%d/dingtalk-hook" % port)
    hook_config = t00ls_sign.load_config(hook_cfg)
    t00ls_sign.setup_logging(False, "")
    t00ls_sign.Notifier(hook_config).send("T00ls 签到：签到成功", "结果：签到成功")
    ding = [raw for path, raw in CAPTURED if path.startswith("/dingtalk-hook")]
    print("       钉钉 payload:", ding[0][:120] if ding else "-")
    ok_ding = bool(ding) and json.loads(ding[0]).get("msgtype") == "text" \
        and "T00ls" in json.loads(ding[0])["text"]["content"]
    results.append(ok_ding)

    # 17h. Bark payload 形状（标题含空格/中文，必须走 JSON body 而不是拼进 URL）
    reset(signed_today=True)
    bark_cfg = os.path.join(tmp, "bark.ini")
    write_config(bark_cfg, notify=True, channels="bark")
    with open(bark_cfg, "a", encoding="utf-8") as fp:
        fp.write("bark_url = http://127.0.0.1:%d/bark/KEY\n" % port)
    bark_config = t00ls_sign.load_config(bark_cfg)
    t00ls_sign.setup_logging(False, "")
    t00ls_sign.Notifier(bark_config).send("T00ls 签到：签到成功", "结果：签到成功")
    bark = [raw for path, raw in CAPTURED if path.startswith("/bark/KEY")]
    print("       Bark payload:", bark[0][:120] if bark else "-")
    ok_bark = bool(bark) and json.loads(bark[0]).get("title", "").startswith("T00ls")
    results.append(ok_bark)

    # 17i. 状态文件损坏 -> 警告并按首次运行处理，不崩
    reset()
    with open(STATE_FILE, "w", encoding="utf-8") as fp:
        fp.write("{ 这不是合法 JSON")
    write_config(cfg, bu=True)
    results.append(run_case(
        "17i 状态文件损坏", ["-c", cfg, "--base-url", base], 0,
        expect=["状态文件读取失败", "没有基线"], forbid=["Traceback"],
    ))

    # 17j. 状态文件里的累计次数是字符串也要能用
    reset()
    write_state(last_sign_date=jdate(2), last_sign_times="41")
    write_config(cfg, bu=True)
    results.append(run_case(
        "17j 状态里数字是字符串", ["-c", cfg, "--base-url", base], 0,
        expect=["检测到漏签 1 天", "补签：已提交"],
    ))
    results.append(STATE["counts"].get("busign", 0) == 1)

    # 17k. --check 与 --bu-sign 同时给出：只读优先，不能花钱
    reset()
    write_state(last_sign_date=jdate(2), last_sign_times=41)
    write_config(cfg, bu=True)
    results.append(run_case(
        "17k --check 优先于 --bu-sign", ["-c", cfg, "--base-url", base, "--check", "--bu-sign"], 0,
        expect=["今日尚未签到"],
    ))
    results.append(STATE["counts"].get("busign", 0) == 0)

    # 19. --test-notify / 钉钉加签
    print("\n--- 通知自检（--test-notify 与钉钉加签） ---")

    # 19a. 一条命令验证通知，且完全不碰站点（配置里故意不写账号）
    reset()
    notify_cfg = os.path.join(tmp, "notify.ini")
    write_config(notify_cfg, username="", password="", cookie="", notify=True,
                 channels="dingtalk", hook="http://127.0.0.1:%d/dingtalk-hook" % port)
    del CAPTURED[:]
    results.append(run_case(
        "19a --test-notify 可用且不访问站点",
        ["-c", notify_cfg, "--test-notify"], 0,
        expect=["通知已发送：dingtalk", "测试通知已发出", "（北京时间）"],
        forbid=["登录成功", "未配置登录信息"],
    ))
    print("       站点请求次数(都应为 0):", {k: v for k, v in STATE["counts"].items()})
    results.append(not any(k in STATE["counts"] for k in ("login", "profile", "sign", "busign")))
    ding_payload = [raw for path, raw in CAPTURED if path.startswith("/dingtalk-hook")]
    results.append(bool(ding_payload) and "T00ls" in json.loads(ding_payload[0])["text"]["content"])
    print("       已捕获测试消息:", (ding_payload[0][:100] if ding_payload else "-"))

    # 19b. 通知没开时给出明确指引，退出码 2
    reset()
    off_cfg = os.path.join(tmp, "notify_off.ini")
    write_config(off_cfg, username="", password="", cookie="")
    results.append(run_case(
        "19b 通知未配置时测试命令", ["-c", off_cfg, "--test-notify"], 2,
        expect=["没有可用的通知渠道", "enabled="],
    ))

    # 19c. 钉钉返回 310000（关键词/加签/IP 白名单）时应翻成人话
    reset()
    err_cfg = os.path.join(tmp, "notify_err.ini")
    write_config(err_cfg, username="", password="", cookie="", notify=True,
                 channels="dingtalk", hook="http://127.0.0.1:%d/dingtalk-error" % port)
    results.append(run_case(
        "19c 钉钉 310000 错误提示", ["-c", err_cfg, "--test-notify"], 1,
        expect=["errcode=310000", "自定义关键词", "加签", "测试通知发送失败"],
    ))

    # 19d. 加签算法：独立重算一遍 HMAC-SHA256 校验
    webhook = "https://oapi.dingtalk.com/robot/send?access_token=abc123"
    secret = "SECtestsecret"
    signed = t00ls_sign.Notifier.dingtalk_signed_url(webhook, secret)
    parsed = urlsplit(signed)
    query = parse_qs(parsed.query)
    ts = query.get("timestamp", [""])[0]
    sign = query.get("sign", [""])[0]
    # 注意 parse_qs 会先把值解码，所以这里要跟“编码前的 base64”比较
    expected_raw = base64.b64encode(
        hmac.new(secret.encode(), ("%s\n%s" % (ts, secret)).encode(), hashlib.sha256).digest()
    ).decode()
    expected_encoded = quote_plus(expected_raw)
    print("       加签 URL:", signed[:96], "...")
    for label, ok in (
        ("保留 access_token", parsed.query.startswith("access_token=abc123&")),
        ("timestamp 是 13 位毫秒", len(ts) == 13 and ts.isdigit()),
        ("sign 是 URL 编码后的 base64", expected_encoded in signed),
        ("sign 与独立重算一致", sign == expected_raw),
    ):
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
        results.append(ok)

    # 19e. 加签端到端：真实发出的 URL 必须带正确的 timestamp 与 sign
    reset()
    signed_cfg = os.path.join(tmp, "notify_signed.ini")
    write_config(signed_cfg, username="", password="", cookie="", notify=True,
                 channels="dingtalk", hook="http://127.0.0.1:%d/dingtalk-hook" % port)
    with open(signed_cfg, "a", encoding="utf-8") as fp:
        fp.write("dingtalk_secret = SECe2e-test-secret\n")
    del CAPTURED[:]
    results.append(run_case(
        "19e 加签后端到端可用", ["-c", signed_cfg, "--test-notify"], 0,
        expect=["通知已发送：dingtalk"],
    ))
    signed_paths = [p for p, _ in CAPTURED if p.startswith("/dingtalk-hook")]
    print("       实际请求 URL:", signed_paths[0] if signed_paths else "-")
    query_e2e = parse_qs(urlsplit(signed_paths[0]).query) if signed_paths else {}
    ts_e2e = query_e2e.get("timestamp", [""])[0]
    sign_e2e = query_e2e.get("sign", [""])[0]
    # 按钉钉官方示例的算法独立重算：hmac key = secret，消息 = "{timestamp}\n{secret}"
    expect_sign = base64.b64encode(hmac.new(
        b"SECe2e-test-secret",
        ("%s\nSECe2e-test-secret" % ts_e2e).encode(),
        hashlib.sha256,
    ).digest()).decode()
    for label, ok in (
        ("URL 带 13 位毫秒 timestamp", len(ts_e2e) == 13 and ts_e2e.isdigit()),
        ("URL 带 sign 且与官方算法一致", bool(sign_e2e) and sign_e2e == expect_sign),
    ):
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
        results.append(ok)

    # 19f. webhook 里已带 timestamp/sign（直接粘了示例 URL）不能出现重复参数
    dirty = "https://oapi.dingtalk.com/robot/send?access_token=tok123&timestamp=111&sign=old"
    cleaned = t00ls_sign.Notifier.dingtalk_signed_url(dirty, "SECabc")
    query_clean = parse_qs(urlsplit(cleaned).query)
    print("       清洗后的 URL:", cleaned[:104], "...")
    for label, ok in (
        ("access_token 保留", query_clean.get("access_token") == ["tok123"]),
        ("timestamp 只剩一个且是新算的",
         len(query_clean.get("timestamp", [])) == 1 and query_clean["timestamp"][0] != "111"),
        ("sign 只剩一个且是新算的",
         len(query_clean.get("sign", [])) == 1 and query_clean["sign"][0] != "old"),
    ):
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
        results.append(ok)

    # 20. 北京 00:05 刚过零点：站点可能还没换日
    print("\n--- 零点边界（站点未换日 / 已换日） ---")

    # 20a. 竞态：读状态时还是昨天的 sign_today，之后站点换日 -> 重试后签到成功
    reset(signed_today=True, rollover_after_profiles=1)
    write_state(last_sign_date=jdate(1), last_sign_times=41)
    write_config(cfg)
    results.append(run_case(
        "20a 站点换日后重试签到成功",
        ["-c", cfg, "--base-url", base, "--stale-retries", "3", "--stale-wait", "0"], 0,
        expect=["站点很可能还没换日", "站点已换日：第 1 次尝试签到成功", "完成：签到成功"],
        forbid=["今日已签到，无需重复签到"],
    ))
    print("       sign 提交次数(应为 1):", STATE["counts"].get("sign", 0))
    results.append(STATE["counts"].get("sign", 0) == 1)

    # 20b. 站点一直没换日 -> 重试到上限后按失败结束（不能静默当成已签到）
    reset(signed_today=True, sign_status="alreadysign")
    write_state(last_sign_date=jdate(1), last_sign_times=41)
    write_config(cfg)
    results.append(run_case(
        "20b 站点始终没换日则报失败",
        ["-c", cfg, "--base-url", base, "--stale-retries", "2", "--stale-wait", "0"], 1,
        expect=["站点似乎还没换日", "本次未能签到"],
        forbid=["完成：今日已签到"],
    ))
    print("       sign 提交次数(应为 2):", STATE["counts"].get("sign", 0))
    results.append(STATE["counts"].get("sign", 0) == 2)

    # 20c. 累计次数已增加（比如用 App 签过了）-> 不该误判成未换日、不该重试
    reset(signed_today=True)
    STATE["sign_times"] = "42"
    write_state(last_sign_date=jdate(1), last_sign_times=41)
    write_config(cfg)
    results.append(run_case(
        "20c 计数已增加则不误判",
        ["-c", cfg, "--base-url", base, "--stale-retries", "3", "--stale-wait", "0"], 0,
        expect=["今日已签到，无需重复签到"],
        forbid=["站点很可能还没换日"],
    ))
    print("       sign 提交次数(应为 0):", STATE["counts"].get("sign", 0))
    results.append(STATE["counts"].get("sign", 0) == 0)

    # 20d. --check 是只读的：即使怀疑没换日也不能提交签到
    reset(signed_today=True, sign_status="alreadysign")
    write_state(last_sign_date=jdate(1), last_sign_times=41)
    write_config(cfg)
    results.append(run_case(
        "20d --check 不触发换日重试",
        ["-c", cfg, "--base-url", base, "--check"], 0,
        expect=["今日已签到，无需重复签到"],
        forbid=["站点很可能还没换日"],
    ))
    results.append(STATE["counts"].get("sign", 0) == 0)

    # 20e. --stale-retries 0：直接判定失败，不重试也不崩
    reset(signed_today=True, sign_status="alreadysign")
    write_state(last_sign_date=jdate(1), last_sign_times=41)
    write_config(cfg)
    results.append(run_case(
        "20e --stale-retries 0",
        ["-c", cfg, "--base-url", base, "--stale-retries", "0", "--stale-wait", "0"], 1,
        expect=["本次未能签到"], forbid=["Traceback"],
    ))
    results.append(STATE["counts"].get("sign", 0) == 0)

    # 20f. 纯函数：looks_like_stale_sign_today 各种边界
    st = t00ls_sign.State(os.path.join(tmp, "probe-state.json"))
    st.data = {"last_sign_date": jdate(1), "last_sign_times": 41}
    for label, ok in (
        ("昨天签过且计数未变 -> 疑似残留",
         t00ls_sign.looks_like_stale_sign_today(st, {"sign_times": "41"}, t00ls_sign.today_cst()) is True),
        ("计数已变 -> 不疑似",
         t00ls_sign.looks_like_stale_sign_today(st, {"sign_times": "42"}, t00ls_sign.today_cst()) is False),
        ("上次签到不是昨天 -> 不疑似",
         t00ls_sign.looks_like_stale_sign_today(
             st, {"sign_times": "41"}, t00ls_sign.date_cst(2)) is False),
    ):
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
        results.append(ok)
    st.data = {"last_sign_times": 41}
    results.append(t00ls_sign.looks_like_stale_sign_today(st, {"sign_times": "41"}, t00ls_sign.today_cst()) is False)
    print("  [PASS] 没有基线日期 -> 不疑似")
    st.data = {"last_sign_date": jdate(1)}
    results.append(t00ls_sign.looks_like_stale_sign_today(st, {"sign_times": "41"}, t00ls_sign.today_cst()) is False)
    print("  [PASS] 站点没给累计次数 -> 不疑似（保守）")

    # 21. 纯环境变量模式：完全没有配置文件，凭据只从环境变量读取
    print("\n--- 纯环境变量模式（不写配置文件） ---")
    reset()
    del CAPTURED[:]
    env_keys = [
        "T00LS_USERNAME", "T00LS_PASSWORD", "T00LS_PASSWORD_MD5", "T00LS_QUESTION_ID",
        "T00LS_QUESTION_ANSWER", "T00LS_COOKIE", "T00LS_BASE_URL", "T00LS_STATE_FILE",
        "T00LS_CONFIG", "T00LS_NOTIFY_ENABLED", "T00LS_NOTIFY_CHANNELS",
        "T00LS_DINGTALK_WEBHOOK", "T00LS_AUTO_BU_SIGN",
    ]
    saved_env = {k: os.environ.get(k) for k in env_keys}
    actions_state = os.path.join(tmp, "actions-state.json")
    for k in env_keys:
        os.environ.pop(k, None)
    os.environ.update({
        "T00LS_USERNAME": USERNAME,
        "T00LS_PASSWORD": PASSWORD,          # 明文，脚本内部转 MD5
        "T00LS_QUESTION_ID": str(QUESTION_ID),
        "T00LS_QUESTION_ANSWER": QUESTION_ANSWER,
        "T00LS_BASE_URL": base,
        "T00LS_STATE_FILE": actions_state,
        "T00LS_NOTIFY_ENABLED": "true",
        "T00LS_NOTIFY_CHANNELS": "dingtalk",
        "T00LS_DINGTALK_WEBHOOK": "http://127.0.0.1:%d/dingtalk-hook" % port,
    })
    try:
        results.append(run_case(
            "21a 纯环境变量即可签到（不带 -c）", [], 0,
            expect=[
                "登录成功", "完成：签到成功",
                "已启用通知渠道：dingtalk", "通知已发送：dingtalk",
            ],
            forbid=["未配置登录信息", "配置文件不存在"],
        ))
        for label, ok in (
            ("配置里没有 config.ini 也能跑通", True),
            ("钉钉通知在无配置文件时同样生效",
             any(p.startswith("/dingtalk-hook") for p, _ in CAPTURED)),
            ("状态文件写到了 T00LS_STATE_FILE 指定的位置", os.path.exists(actions_state)),
            ("没设 T00LS_AUTO_BU_SIGN 就不会乱花 TuBi",
             STATE["counts"].get("busign", 0) == 0),
        ):
            print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
            results.append(ok)
        print("       状态文件内容:", open(actions_state, encoding="utf-8").read().replace("\n", " ").strip()[:120])
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 22. --show-config 配置诊断（定位"钉钉不推送"这类问题）
    print("\n--- 配置诊断 --show-config ---")

    # 22a. 配置齐全 -> 明确给出"已就绪"，且不碰站点、不泄漏 token
    reset()
    diag_ok = os.path.join(tmp, "diag_ok.ini")
    write_config(diag_ok, notify=True, channels="dingtalk",
                 hook="http://127.0.0.1:%d/dingtalk-hook?access_token=TOP_SECRET_TOKEN" % port)
    results.append(run_case(
        "22a 配置齐全时结论为已就绪", ["-c", diag_ok, "--show-config"], 0,
        expect=["通知开关    : enabled = true", "已启用通知渠道：dingtalk", "通知已就绪"],
        forbid=["TOP_SECRET_TOKEN"],
    ))
    print("       站点请求次数(应为 0):", STATE["counts"])
    results.append(not any(k in STATE["counts"] for k in ("login", "profile", "sign")))

    # 22b. 填了 webhook 却忘了开 enabled —— 最容易踩的坑
    reset()
    diag_bad = os.path.join(tmp, "diag_bad.ini")
    write_config(diag_bad, channels="dingtalk",
                 hook="http://127.0.0.1:%d/dingtalk-hook?access_token=TOP_SECRET_TOKEN" % port)
    results.append(run_case(
        "22b 填了 webhook 却没开 enabled", ["-c", diag_bad, "--show-config"], 0,
        expect=["enabled = false", "不会推送任何通知", "忘了打开 enabled"],
        forbid=["TOP_SECRET_TOKEN"],
    ))

    # 22c. 正常签到流程里也要提示这条（否则用户永远不知道通知没开）
    reset()
    write_config(cfg, channels="dingtalk", hook="http://127.0.0.1:%d/dingtalk-hook" % port)
    results.append(run_case(
        "22c 签到时会警告开关没开", ["-c", cfg, "--base-url", base], 0,
        expect=["但 [notify] enabled = false", "完成：签到成功"],
    ))
    print("       实际发出的通知数(应为 0):", len(CAPTURED))
    results.append(not CAPTURED)

    # 22d. enabled=true 但没写 channels
    reset()
    diag_nochan = os.path.join(tmp, "diag_nochan.ini")
    write_config(diag_nochan, notify=True)
    results.append(run_case(
        "22d 开了开关却没写 channels", ["-c", diag_nochan, "--show-config"], 0,
        expect=["channels = (空)", "channels 是空的", "不会推送任何通知"],
    ))

    # 18. 纯函数边界检查
    print("\n--- 纯函数边界 ---")
    unit = [
        ("days_between 正常", t00ls_sign.days_between("2026-10-01", "2026-10-03") == 2),
        ("days_between 非法输入", t00ls_sign.days_between("oops", "2026-10-03") is None),
        ("days_between None", t00ls_sign.days_between(None, "2026-10-03") is None),
        ("as_int 字符串", t00ls_sign.as_int(" 41 ") == 41),
        ("as_int 非法", t00ls_sign.as_int("abc") is None),
        ("as_int None", t00ls_sign.as_int(None) is None),
        ("as_int 负数", t00ls_sign.as_int("-20") == -20),
        ("collect_strings 嵌套", "x" in t00ls_sign.collect_strings({"a": [{"b": "x"}]})),
        ("mask 短串", t00ls_sign.mask("ab") == "**"),
        ("normalize_cookie 去重", t00ls_sign.normalize_cookie("Cookie: a=1;\nb=2;\na=1") == "a=1; b=2"),
        ("normalize_cookie 空", t00ls_sign.normalize_cookie("") == ""),
        ("today_cst 格式", len(t00ls_sign.today_cst()) == 10),
    ]
    for label, ok in unit:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
        results.append(ok)

    redirect_server.shutdown()
    chain_server.shutdown()
    server.shutdown()
    ok = all(results)
    print("\n==== %d/%d 项通过 ====" % (sum(1 for r in results if r), len(results)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
