#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉钉机器人 Webhook 探测工具（只用标准库，不依赖 requests）

作用：钉钉返回 errcode=310000 时，只说明"安全设置没通过"，不告诉你到底是
「自定义关键词」不匹配、「加签」没签对、还是「IP 白名单」没放行。这个工具会

    1) 先用【不带加签】的方式发一条
    2) 再（如果给了 secret）用【带加签】的方式发一条

然后打印两次的原始返回，你就能直接看出机器人到底用的是哪种模式：
    - 只有第 1 次成功  -> 机器人用的是「自定义关键词」或没开安全设置
    - 只有第 2 次成功  -> 机器人用的是「加签」-> 必须把 secret 填进 config.ini
    - 两次都失败       -> 多半是「IP 白名单」模式，或 token 复制错了

用法：
    python3 tools/dingtalk-check.py --token <access_token> [--secret <SEC...>]
    # token 也可以整个 webhook 地址直接传
    python3 tools/dingtalk-check.py --token 'https://oapi.dingtalk.com/robot/send?access_token=xxx' --secret 'SECxxx'

参数也可以来自环境变量：T00LS_DINGTALK_WEBHOOK / T00LS_DINGTALK_SECRET
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://oapi.dingtalk.com/robot/send"
CONTENT = "T00ls 签到：Webhook 探测（这条消息用于确认机器人安全设置）"

HINTS = {
    "300001": "access_token 无效：Webhook 地址不完整，或机器人已被删除",
    "310000": "安全设置未通过：关键词没匹配 / 加签不对 / IP 不在白名单",
    "130101": "发送太快被钉钉限流，稍等再试",
}


def build_url(webhook: str, secret: str = "") -> str:
    """按钉钉官方算法（可选）拼上加签参数，并清掉 URL 里已有的 timestamp/sign。"""
    if "access_token=" not in webhook:
        webhook = "%s?access_token=%s" % (API, webhook)
    parts = urllib.parse.urlsplit(webhook)
    kept = [
        (k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k not in ("timestamp", "sign")
    ]
    query = urllib.parse.urlencode(kept)
    base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    if not secret:
        return base

    timestamp = str(int(time.time() * 1000))
    string_to_sign = "%s\n%s" % (timestamp, secret)
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
    return "%s&timestamp=%s&sign=%s" % (base, timestamp, sign)


def send(webhook: str, secret: str = "", content: str = CONTENT, timeout: float = 15.0) -> dict:
    """发送一条文本消息，返回钉钉的原始 JSON（网络异常时返回 {"_error": ...}）。"""
    url = build_url(webhook, secret)
    body = json.dumps({"msgtype": "text", "text": {"content": content}}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json;charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return {"_error": "HTTP %s %s" % (exc.code, raw[:200])}
    except Exception as exc:  # 网络层错误
        return {"_error": str(exc)}
    try:
        return json.loads(raw)
    except ValueError:
        return {"_error": "返回不是 JSON：%s" % raw[:200]}


def describe(result: dict) -> str:
    if "_error" in result:
        return "请求失败：%s" % result["_error"]
    code = str(result.get("errcode", "?"))
    errmsg = result.get("errmsg", "")
    if code == "0":
        return "成功（errcode=0 %s）" % errmsg
    return "失败（errcode=%s errmsg=%s）%s" % (
        code, errmsg, HINTS.get(code, "")
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="探测钉钉机器人用的是哪种安全设置（关键词 / 加签 / IP 白名单）"
    )
    parser.add_argument("--token", default=os.environ.get("T00LS_DINGTALK_WEBHOOK", ""),
                        help="access_token，或整条 webhook 地址")
    parser.add_argument("--secret", default=os.environ.get("T00LS_DINGTALK_SECRET", ""),
                        help="加签密钥（SEC 开头）；机器人若用关键词模式则不用给")
    parser.add_argument("--content", default=CONTENT, help="测试消息内容")
    args = parser.parse_args(argv)

    if not args.token:
        parser.error("需要 --token（或环境变量 T00LS_DINGTALK_WEBHOOK）")

    print("将发送两条探测消息，请留意钉钉群里是否收到：")
    print("  1) 不带加签")
    print("  2) 带加签" + ("（已提供 secret）" if args.secret else "（未提供 secret，跳过）"))
    print("-" * 66)

    plain = send(args.token, "", args.content)
    print("① 不带加签 : %s" % describe(plain))
    print("   原始返回 : %s" % json.dumps(plain, ensure_ascii=False))

    signed_ok = None
    if args.secret:
        signed = send(args.token, args.secret, args.content)
        signed_ok = "_error" not in signed and str(signed.get("errcode")) == "0"
        print("② 带加签   : %s" % describe(signed))
        print("   原始返回 : %s" % json.dumps(signed, ensure_ascii=False))
    print("-" * 66)

    plain_ok = "_error" not in plain and str(plain.get("errcode")) == "0"
    if plain_ok and signed_ok is True:
        print("结论：两种方式都被接受 —— 机器人没有强制加签（多半用「自定义关键词」）。")
        print("      -> config.ini 里 dingtalk_secret 留空即可（填了也能过，但没必要）")
        return 0
    if signed_ok:
        print("结论：只有【带加签】被接受 —— 机器人用的是「加签」模式。")
        print("      -> config.ini 里必须把 dingtalk_secret 填成这个密钥；")
        print("         同时确认服务器时间准确（加签允许 ±1 小时偏差，用 timedatectl 查看）")
        return 0
    if plain_ok:
        print("结论：只有【不带加签】被接受 —— 机器人用的是「自定义关键词」或没开安全设置。")
        print("      -> config.ini 里 dingtalk_secret 必须留空；")
        print("         若开了「自定义关键词」，关键词必须是 T00ls（区分大小写）")
        return 0
    print("结论：两种方式都被拒绝 —— 多半是「IP 白名单」模式，或 access_token 复制错了。")
    print("      -> 钉钉里把安全设置改成「自定义关键词」(填 T00ls) 或「加签」（本脚本都支持），")
    print("         IP 白名单模式本脚本不支持（服务器没有固定出口 IP）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
