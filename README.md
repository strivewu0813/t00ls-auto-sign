# T00ls 每日自动签到（Ubuntu）

在 Ubuntu 服务器上每天自动完成 [www.t00ls.com](https://www.t00ls.com) 的签到，支持 **钉钉/企业微信/Server酱/Bark/Telegram/邮件通知** 和 **自动补签**，以及 systemd 定时器 / crontab、失败重试。

脚本基于 **T00ls 官方开放 API**（官方 2020-03-24 公告《[T00ls开放官方API接口](https://www.t00ls.com/articles-55554.html)》，接口文档 <https://www.t00ls.com/api.html>），不是抓包模拟浏览器，请求量极低（每天最多 3 次触发，已签到即退出）。

## 一、文件清单

| 文件 | 作用 |
| --- | --- |
| `t00ls_sign.py` | 主脚本（Python 3.8+，只依赖 `requests`） |
| `deploy.sh` | Ubuntu 一键部署：venv + 依赖 + systemd timer（或 cron） |
| `config.example.ini` | 配置模板，复制成 `config.ini` 后填写 |
| `requirements.txt` | 依赖列表（`requests`） |
| `tests/selftest.py` | 本地自检：起 mock 接口跑 90 个场景（登录/签到/重试/Cookie/补签/通知/跳转/异常），不碰真实站点 |

自检用法（改完代码后可以随时验证，不会向 t00ls 发任何请求）：

```bash
pip3 install -r requirements.txt
python3 tests/selftest.py     # 期望最后输出 90/90 项通过
```

## 二、快速开始（推荐：一键脚本）

把 5 个文件上传到服务器同一目录，然后：

```bash
chmod +x deploy.sh
sudo ./deploy.sh
```

脚本会依次：创建 `/opt/t00ls-sign` 目录 → 建 venv 装 `requests` → 交互式问你账号 / 密码 / 安全提问 → 问钉钉 Webhook（可留空）→ 问是否开启自动补签 → 写入 `config.ini`（权限 600）→ 安装并启用 `t00ls-sign.timer`。

> 部署脚本会自己处理缺 `python3-venv`、以及上次中断留下的"有 python 没 pip"的半成品虚拟环境：自动 `apt-get install python3-venv`、自动重建 venv。装不上时也会直接告诉你需要手动执行哪条命令，不会留下 `venv/bin/pip: No such file or directory` 这种看不懂的报错。

装完立刻验证一次：

```bash
sudo ./deploy.sh --test        # 立即签到一次并打印详细日志
sudo ./deploy.sh --status      # 看定时器下次触发时间 + 最近日志
sudo ./deploy.sh --update      # 升级：只替换程序文件，不动配置和定时器
```

> 不想用 systemd（比如容器里）就加 `--cron`：`sudo ./deploy.sh --cron`

### 升级到新版本（只换程序文件）

拿到新版 `t00ls_sign.py` 后**必须重新部署**，否则 `/opt/t00ls-sign/` 里跑的还是旧文件（会出现"新选项不被识别"这类现象）：

```bash
# 1) 覆盖上传新的 t00ls_sign.py 到原来那个目录
# 2) 只更新程序文件，配置与定时器都不动
cd /root/t00ls && sudo ./deploy.sh --update
```

`--update` 会重新拷贝脚本、确认依赖，然后打印**新版的版本号和全部可用选项**，一眼就能确认换成功了。

没把握部署的是哪一版？两种确认方式：

```bash
# 方式一：直接问脚本自己
/opt/t00ls-sign/venv/bin/python /opt/t00ls-sign/t00ls_sign.py --version

# 方式二：每次运行的日志第一行就写着版本和正在执行的脚本路径
journalctl -u t00ls-sign -n 20 --no-pager | head -3
# [INFO] t00ls_sign v1.3.0 启动（脚本：/opt/t00ls-sign/t00ls_sign.py）
```

## 三、配置说明（`config.ini`）

### 方式一：账号密码 + 安全提问（推荐）

```ini
[t00ls]
username = your_username
password = your_password       ; 明文即可，脚本内部转 32 位小写 MD5
question_id = 0                ; 安全提问编号，没有就填 0
question_answer =
```

安全提问编号对照：

| 编号 | 问题 | 编号 | 问题 |
| --- | --- | --- | --- |
| 0 | 没有安全提问 | 4 | 您其中一位老师的名字 |
| 1 | 母亲的名字 | 5 | 您个人计算机的型号 |
| 2 | 爷爷的名字 | 6 | 您最喜欢的餐馆名称 |
| 3 | 父亲出生的城市 | 7 | 驾驶执照的最后四位数字 |

也可以用现成的 MD5（填了 `password_md5` 就以它为准）：

```bash
echo -n '你的密码' | md5sum      # 取输出的 32 位小写串
```

### 方式二：Cookie（不想存密码 / 登录接口要求验证码时用）

浏览器登录 T00ls → `F12` → `Network` → 随便点一个请求 → 复制 Request Headers 里的 `Cookie` 整行，填到：

```ini
[t00ls]
cookie = xxxxx_saltkey=xxx; xxxxx_auth=xxx; ...
```

脚本启动时会先用 `members-profile.json` 校验 Cookie，失效会明确报 `loginfirst`，重新复制一份即可（Cookie 通常能撑几十天）。

> 两种方式都填时 **Cookie 优先**。

### 其他配置项

```ini
timeout = 20                                        ; 单次请求超时（秒）
retries = 3                                         ; 网络失败重试次数（指数退避）
log_file = /var/log/t00ls-sign.log                  ; 留空则只输出到 stdout
state_file =                                        ; 留空 = 配置文件同目录下的 t00ls-state.json
auto_bu_sign = false                                ; 自动补签（见第七节），每次消耗 20 TuBi
bu_sign_within_days = 3                             ; 漏签超过这么多天就不自动补
```

> `deploy.sh` 生成的 `config.ini` 会把日志写到安装目录（`/opt/t00ls-sign/t00ls-sign.log`），两种都行；systemd 方式下 `journalctl -u t00ls-sign` 也能看到同样的内容。

所有敏感项都能用环境变量覆盖，便于不落盘保存密码：
`T00LS_USERNAME`、`T00LS_PASSWORD`、`T00LS_PASSWORD_MD5`、`T00LS_QUESTION_ID`、`T00LS_QUESTION_ANSWER`、`T00LS_COOKIE`、`T00LS_CONFIG`、`T00LS_DINGTALK_WEBHOOK`、`T00LS_AUTO_BU_SIGN` 等。

## 四、手动运行

```bash
# 直接跑（幂等：已签到会直接退出，退出码 0）
/opt/t00ls-sign/venv/bin/python /opt/t00ls-sign/t00ls_sign.py -c /opt/t00ls-sign/config.ini

# 只查今天签没签、TuBi / 累计签到次数
... t00ls_sign.py -c config.ini --check

# 演练：只登录 + 查状态，不提交签到
... t00ls_sign.py -c config.ini --dry-run

# 强制补签一次（不检查漏签，消耗 20 TuBi；用来核对该接口的真实行为）
... t00ls_sign.py -c config.ini --bu-sign -v

# 只发一条测试通知（不登录、不访问站点），用来验证钉钉等渠道
... t00ls_sign.py -c config.ini --test-notify

# 本次不补签（临时覆盖配置里的 auto_bu_sign）
... t00ls_sign.py -c config.ini --no-bu-sign

# 详细日志
... t00ls_sign.py -c config.ini -v

# 查当前部署的版本
... t00ls_sign.py --version
```

退出码：`0` = 签到成功或今日已签到；`1` = 接口/网络/登录失败；`2` = 配置错误。

## 五、定时任务

### 定时时间怎么设（服务器在美国也没问题）

时间一律按**北京时间**指定，`deploy.sh` 会把它写进带 `Asia/Shanghai` 的 systemd 日历表达式，**服务器在哪个时区都不影响**：

```bash
# 交互式安装时会被问到（直接回车用默认 09:37,13:07,21:07）
sudo ./deploy.sh

# 或者用环境变量直接指定（北京时间，逗号分隔）
sudo T00LS_SCHEDULE="00:05,00:35" ./deploy.sh
```

**想要北京时间每天 00:05 签到，就填 `00:05,00:35`。** 为什么要带第二个时间？因为**刚过零点是全天最危险的时刻**：

> 站点如果在 00:00 之后要过一会儿才把签到状态清零，脚本 00:05 去查会看到**昨天的** `sign_today=1`，如果直接当成"今天已签到"就会**一整天都不再签到**（静默漏签，最难发现）。

脚本对这种情况有专门处理（不是靠猜，靠数据判断）：

- **判断依据**：`sign_today=1` 且**站点累计签到次数和昨天一模一样**（说明今天确实还没产生签到记录）且我们昨天刚签过；
- **处理方式**：等一会儿重新提交签到（默认最多 3 次、每次间隔 120 秒，共约 6 分钟）。这个时间点直接调签到接口是安全的——真没换日时接口只会回 `alreadysign`，不会重复计数；
- **等到站点换日**：立刻签到成功，日志写 `站点已换日：第 1 次尝试签到成功`；
- **一直没换日**：按**失败**退出（退出码 1）并推送通知，而不是假装成功——这样你不会被静默漏签蒙在鼓里；
- **你其实已经用 App 签过了**：此时累计次数会增加，条件不成立，脚本会正常当作"今日已签到"，不会误报。

配置项（`config.ini`，也可用 `--stale-retries` / `--stale-wait` 临时覆盖）：

```ini
[t00ls]
stale_retries = 3      ; 怀疑站点没换日时的重试次数
stale_wait = 120       ; 每次重试前等待的秒数
```

### systemd timer（`deploy.sh` 默认，推荐）

按上面的时间生成的 `/etc/systemd/system/t00ls-sign.timer`（以 `00:05,00:35` 为例）：

```ini
[Timer]
OnCalendar=*-*-* 00:05:00 Asia/Shanghai
OnCalendar=*-*-* 00:35:00 Asia/Shanghai
RandomizedDelaySec=600
Persistent=true
```

`deploy.sh` 装完会**直接把解析后的触发时间打出来**，可以立刻核对：

```
[+] 每天触发时间（北京时间）：00:05,00:35
    *-*-* 00:05:00 Asia/Shanghai
      Next elapse: Thu 2026-10-03 00:05:00 CST
    *-*-* 00:35:00 Asia/Shanghai
      Next elapse: Thu 2026-10-03 00:35:00 CST
```

`Next elapse` 后面会同时给出服务器本地时间与 UTC，服务器在美国应该能看到类似 `11:05 CDT` / `16:05 UTC` 的换算结果 —— 这就是"北京时间 00:05"。

设计要点：

- **脚本幂等**：已签到立即退出，所以多设几个时间点等于免费重试，网络抖动不影响当天签到。
- **时区显式写死 `Asia/Shanghai`**，服务器是 UTC 或美东都不会签错日期；老版本 systemd 不认时区后缀时 `deploy.sh` 会自动按 UTC+8 换算成 UTC 时刻（`00:05` → `16:05 UTC`）。
- `RandomizedDelaySec` 让实际请求时刻随机漂移，避免每天固定秒级打卡。
- `TimeoutStartSec=900` 是给"站点尚未换日"的重试留出的时间。

常用命令：

```bash
systemctl list-timers t00ls-sign.timer       # 下次触发时间
sudo systemctl start t00ls-sign.service      # 立即跑一次
journalctl -u t00ls-sign -n 50 --no-pager    # 查看日志
systemd-analyze calendar "*-*-* 00:05:00 Asia/Shanghai"   # 手动核对换算
timedatectl                                  # 看服务器时区与时间是否准
```

> 日志时间戳（`journalctl` 左侧）用的是**服务器本地时间**，而脚本输出的报告里统一标注**北京时间** —— 对不上是正常的，报告那行才是"算作哪一天签的"。

### crontab 备选

```bash
sudo ./deploy.sh --cron          # 会按 T00LS_SCHEDULE / 交互输入生成
```

生成的样式：

```cron
# T00ls 自动签到（北京时间每天 00:05,00:35）
CRON_TZ=Asia/Shanghai
5 0 * * * /opt/t00ls-sign/venv/bin/python /opt/t00ls-sign/t00ls_sign.py -c /opt/t00ls-sign/config.ini >> /opt/t00ls-sign/t00ls-sign.log 2>&1
35 0 * * * /opt/t00ls-sign/venv/bin/python /opt/t00ls-sign/t00ls_sign.py -c /opt/t00ls-sign/config.ini >> /opt/t00ls-sign/t00ls-sign.log 2>&1
```

> ⚠️ cron 对时区的支持不统一（部分实现会忽略 `CRON_TZ`）。服务器在美国时最稳的做法是先执行
> `sudo timedatectl set-timezone Asia/Shanghai`，让 cron 与日志时间都变成北京时间 ——
> 脚本本身不依赖服务器时区，这么做只是为了 cron 解释时间不出错。
> 想省事就用 systemd 方式，它原生支持时区后缀。

## 六、钉钉通知（签到成功就推送）

1. 钉钉里建一个群（只放自己一个人也行）→ **群设置 → 智能群助手 → 添加机器人 → 自定义**
2. **安全设置三选一**（见下表，脚本只支持前两种，推荐第一种）
3. 复制生成的 Webhook 地址，填进配置：

| 安全设置 | 脚本怎么配 |
| --- | --- |
| **自定义关键词**（最简单） | 关键词填 `T00ls`（通知标题固定以 `T00ls` 开头），`dingtalk_secret` 留空 |
| **加签**（推荐，最安全） | 把 `SEC` 开头的密钥填到 `dingtalk_secret`，脚本每次自动算 `timestamp`+`sign` |
| IP 白名单 | **不支持**：脚本从服务器所在网段出网，不固定出口 IP，请改用上面两种 |

两种可以同时开启：脚本发出的消息固定以 `T00ls` 开头，所以关键词也会通过。

```ini
; —— 关键词模式 ——
[notify]
enabled = true
channels = dingtalk
dingtalk_webhook = https://oapi.dingtalk.com/robot/send?access_token=xxxxxxxx

; —— 加签模式（多填一行 secret 即可）——
dingtalk_secret = SECxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

`deploy.sh` 安装时也会直接问你 webhook，填了就会自动写好并打开通知。

### 用「加签」模式：取密钥 → 填配置 → 验证

1. 钉钉群里点机器人头像 → **机器人设置 → 安全设置 → 加签** → 复制那串 `SEC` 开头的密钥（很长，注意别漏字符、别带空格）
2. 填进 `config.ini` 的 `dingtalk_secret`，`dingtalk_webhook` 填**干净的**地址（只带 `access_token`）
3. 验证：

```bash
/opt/t00ls-sign/venv/bin/python /opt/t00ls-sign/t00ls_sign.py -c /opt/t00ls-sign/config.ini --test-notify
```

脚本按钉钉官方算法签名（与官方文档给出的示例代码完全一致）：

```
stringToSign = "{timestamp}\n{secret}"
sign         = urlencode(base64(HmacSHA256(stringToSign, key=secret)))
timestamp    = 毫秒级时间戳
```

加签的三个常见坑，脚本都替你处理或提醒了：

| 坑 | 说明 |
| --- | --- |
| **直接粘了带 `timestamp`/`sign` 的完整 URL** | 很多人照示例把拼好的 URL 存进配置，再签一次就成重复参数、验签失败。脚本会**先清掉旧的 `timestamp`/`sign` 再重新签**（`access_token` 保留） |
| **服务器时间不准** | 加签允许 ±1 小时偏差，时钟漂移超过就会被判 `310000`。报错信息里会提示你跑 `timedatectl status` 检查 NTP |
| **把 secret 和 access_token 弄混** | `access_token` 在 URL 里、`secret`（`SEC…`）单独一行，两者都在机器人设置页，别互相填错 |

`dingtalk_secret` 也支持环境变量 `T00LS_DINGTALK_SECRET`，方便不落盘保存。

### 怎么确认钉钉配置生效

**方法一（推荐）：一条命令自测，不登录、不签到**

```bash
/opt/t00ls-sign/venv/bin/python /opt/t00ls-sign/t00ls_sign.py -c /opt/t00ls-sign/config.ini --test-notify
```

成功输出：

```
[INFO] 已启用通知渠道：dingtalk
[INFO] 即将发送的测试消息内容：
[INFO]     这是一条测试通知：脚本没有登录、也没有签到。
[INFO]     时间：2026-10-02 19:50:59（北京时间）
[INFO]     渠道：dingtalk
[INFO] 通知已发送：dingtalk
[INFO] 测试通知已发出：dingtalk —— 请到钉钉群确认收到了这条消息
```

退出码 `0` = 钉钉接口接受了消息（**再去群里确认真的收到**）；`1` = 发送失败（看上面的警告行）；`2` = 通知没配置好。

> 注意：日常运行时**"今日已签到"是默认不推送的**（免打扰），所以直接跑 `--test` 可能什么都不发，别误以为钉钉坏了。要每次都推就把 `always = true` 打开。

**方法二：看真实签到时的日志**

```bash
journalctl -u t00ls-sign -n 50 --no-pager | grep -E '通知|钉钉'
```

出现 `通知已发送：dingtalk` 就是发出去了。若显示 `通知渠道 dingtalk 发送失败：…`，错误里已经翻成人话：

| 钉钉返回 | 含义 | 处理 |
| --- | --- | --- |
| `errcode=300001` | access_token 无效 | Webhook 复制不全或机器人已被删除 |
| `errcode=310000` | 安全设置未通过 | 关键词没填 `T00ls` / 加签密钥没填 / IP 白名单没放行 |
| `errcode=130101` | 发送太快被限流 | 稍后再试 |
| `HTTP 4xx` / 超时 | 服务器出网问题 | 检查 DNS/代理（代理见第九节） |

**方法三：绕开脚本，直接用 curl 验证 webhook 本身**

用来区分"是钉钉问题"还是"是脚本问题"：

```bash
curl -sS -X POST 'https://oapi.dingtalk.com/robot/send?access_token=你的token' \
  -H 'Content-Type: application/json' \
  -d '{"msgtype":"text","text":{"content":"T00ls 签到：curl 测试"}}'
```

返回 `{"errcode":0,"errmsg":"ok"}` 说明 webhook 与关键词都对，那问题就在脚本侧的配置（比如 `enabled` 没开、`channels` 写错、跑的是另一个 config）。

推送内容示例（签到成功时）：

```
T00ls 签到：签到成功
结果：签到成功
账号：yourname
时间：2026-10-02 09:41:07
今日已签到：是
累计签到：128 次
TuBi：96
TCV：12
补签：已提交（补签请求已提交，TuBi 116 -> 96）
```

**通知规则**：签到成功 → 推送；签到失败 → 推送（含失败原因，方便及时修）；「今日已签到」这种重复触发 → 默认不推送（避免一天 3 条骚扰），但**如果发生了补签就照发**；把 `always = true` 设上则每次运行都推送。

其他渠道（企业微信、Server 酱、Bark、Telegram、邮件）同样在 `[notify]` 里配置，`channels` 可多选逗号分隔：

```ini
channels = dingtalk, serverchan
wecom_webhook = https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
serverchan_key = SCTxxxx
bark_url = https://api.day.app/你的key
telegram_token = 123456:ABC
telegram_chat_id = 123456789
smtp_host = smtp.qq.com
smtp_port = 465
smtp_user = you@qq.com
smtp_password = 邮箱授权码
mail_to = you@qq.com
```

某个渠道没填参数时，脚本启动就会提示「缺少配置 xxx，本次不会发送」并跳过，不会影响签到本身；通知发送失败也从不影响签到结果。

## 七、自动补签（每次消耗 20 TuBi）

T00ls 的限号规则里有一条是「三个月内连续签到少于 90 天」，所以漏签是有代价的。补签接口 `POST /ajax-busign.json`（官方文档第 5 节）每次扣 **20 TuBi**，因此脚本对"什么时候补"非常克制：

```ini
[t00ls]
auto_bu_sign = true        ; 默认 false，开了才会自动补
bu_sign_within_days = 3    ; 漏签超过 3 天就不自动补了
```

**它怎么判断漏签**：不看心情，看站点自己的累计签到次数（`sign_times`）。

```
上次确认签到的日期 D，当天累计签到 N 次
今天 T，签到后累计 M 次
相隔天数 = T - D，期间真实签到 = M - N
两个信号取较小值：
  counter 角度：相隔天数 - 真实签到次数
  记录角度  ：相隔天数 - 1（我们记录里没签过的天数）
```

两个信号不一致通常说明你用过 App / 微信公众号 / TG 机器人签到（那种签到我们看不到），此时取更保守的值 —— **宁可少补，也不冤枉花 20 TuBi**。用「取较小值」还有个好处：本机宕机几天不会造成误判，因为计数来自站点。

三道防止乱花钱的闸门：

1. **检测到真漏签才补**（`--bu-sign` 手动强制除外）；
2. **每天最多补一次** —— 定时器一天跑 3 次，状态文件里记着「今天补过了」，第二、三次触发只会打日志跳过；
3. **先落盘再请求** —— 即使请求过程中异常退出，同一天也不会重试。

另外：`--check` / `--dry-run` 永远不会补签；首次运行没有基线，会明确打日志「首次运行，没有基线」并跳过（从第二天起才能算漏签）。

**手动补签 / 核对接口行为**：

```bash
# 强制补一次（不检查是否真漏签），并把接口原始返回打到日志里
/opt/t00ls-sign/venv/bin/python /opt/t00ls-sign/t00ls_sign.py -c /opt/t00ls-sign/config.ini --bu-sign -v
```

官方文档只给了补签接口的参数、没写全返回值语义，所以脚本每次都把**原始 JSON 返回**和 **TuBi 变化**写进日志和通知（形如 `补签：已提交（…，TuBi 116 -> 96）`）。第一次补签后建议看一眼日志确认符合预期：

```bash
journalctl -u t00ls-sign -n 30 --no-pager | grep 补签
```

## 八、脚本做了什么

```
POST /login.json           action=login, username, password(MD5), questionid, answer  -> formhash
GET  /members-profile.json                                                            -> sign_today / sign_times / extcredits2(TuBi) / formhash
POST /ajax-sign.json       formhash + signsubmit=true                                 -> sign_success / alreadysign
GET  /members-profile.json 复核 sign_today 是否真的变成 1
POST /ajax-busign.json     formhash + signsubmit=true（仅在开了 auto_bu_sign 且检测到漏签时，扣 20 TuBi）
GET  /members-profile.json 复核补签结果与 TuBi 变化
```

细节处理：

- 先查 `sign_today`，已签到直接退出，**绝不重复提交**；
- 接口返回 `wrongsubmit`（formhash 过期）自动刷新 formhash 重试一次；
- 网络错误指数退避重试（默认 3 次），UA / Referer / Accept 都按浏览器习惯设置；
- 接口说成功但 `sign_today` 仍为 0 时按失败处理，交给下一次定时触发重试；
- 跨域跳转（如 `.net` → `.com`）自动跟随并改用真实域名重发 POST，最多跟 3 跳；
- **补签出错不牵连签到结论**：签到已成功而补签失败时，退出码仍是 0、通知标题也仍是「签到成功」，只在报告里写清补签失败原因；
- 报告里的时间统一标为**北京时间**，和"今天是哪天"的判定口径一致；
- 日志记录账号、结果、累计签到、TuBi、补签动作，便于事后核对。

## 九、常见问题

**登录失败返回数字错误码**（`login.json` 实测行为，脚本已自动翻译成人话）

| 返回 | 含义 | 处理 |
| --- | --- | --- |
| `message: 1` | 密码为空 | 检查 `password` / `password_md5` 是否填了 |
| `message: 2` | 用户名为空 | 检查 `username` |
| `message: 3` / `4` | 用户名、密码或安全提问不正确 | 核对密码 MD5 与安全提问编号/答案 |
| `message: "5failedlogin,plswait15min"` | 同 IP 连续失败过多，被限制登录 | 等约 15 分钟再试；修好配置即可 |

> 站点按 IP 限制连续失败登录，所以**密码改对之前不要反复手动跑**。定时任务一天只触发 3 次、间隔数小时，不会踩到这个限制。

**登录失败 `login_question_invalid` / `login_answer_invalid`**
安全提问编号或答案不对。去 T00ls 用浏览器登录时留意站点问的是哪一题；实在不确定就用 Cookie 方式（方式二），或在浏览器 F12 里看到 `questionid` 的实际取值。

**`Cookie 已失效或未登录（loginfirst）`**
Cookie 过期了，重新复制一份浏览器 Cookie。

**`接口未返回 JSON（可能被 CDN/WAF 拦截）`**
站点换域名或被 CDN 拦了。先用 `--base-url https://www.t00ls.com` 显式指定；老脚本里的 `www.t00ls.net` **实测是 301 跳转到 `.com` 的**，而 301 会让 POST 被降级成 GET（丢请求体），所以脚本遇到跨域跳转会**自动切换域名重发**并打警告让你改配置 —— 也就是说 `.net` 也能跑，但建议直接用 `.com`。

**签到成功但 TuBi 没涨？**
按 T00ls 总规则，签到是**连续签到的奇数日期才给 1 TuBi**（周五「疯狂星期五」翻倍），所以有时签到当天不涨币，属正常。可用 `--check` 对照 `sign_times` / `TuBi` 的变化。

**开了自动补签却没补？**
看日志里那行原因，正常有这几种：`首次运行，没有基线`（第二天起才会判断）、`未检测到漏签`（其实没漏）、`超过 bu_sign_within_days`（漏太久，脚本不擅自补）、`今日已经尝试过补签`（当天已补过）。**以上情况都不会扣 TuBi。**

**补签到底扣了多少？**
看通知或日志里的 `TuBi A -> B` 以及原始 JSON 返回 —— 脚本把没把握的东西全部摊开给你看，而不是让你猜。补签被拒时（如 `wrongbusubmit`，多半是没有可补的日期）也不会重试。

**用了脚本是不是就不用自己登录了？**
建议偶尔真的登录一次。总规则写明「TuBi + 存款总额 > 1500 且 30 天未登录，每天扣 10% 的 TuBi，**机器人签到不算登录访问**」，所以攒了很多 TuBi 的账号别完全交给脚本。

**服务器时区是 UTC 会不会签错天？**
不会，定时器里写了 `Asia/Shanghai`；手动跑只影响“今天”的判定时刻，签到本身按论坛服务器时间结算。

**配置文件用记事本改过会不会坏？**
不会，脚本按 `utf-8-sig` 读取，带 BOM 也能正常解析；格式写坏时会明确报“配置文件格式错误”并以退出码 2 结束，不会抛异常栈。

**部署时报 `/opt/t00ls-sign/venv/bin/pip: No such file or directory`**
Ubuntu 上 pip 由 `python3-venv` 提供，缺这个包时 `python3 -m venv` 会"成功"建出目录但**不装 pip**。手动修复三步：

```bash
sudo apt-get update && sudo apt-get install -y python3-venv
sudo rm -rf /opt/t00ls-sign/venv
cd /root/t00ls && sudo ./deploy.sh
```

新版 `deploy.sh` 已会自动探测（探测 `ensurepip` 而不是 `venv --help`）、自动装包、自动重建这种半成品 venv，并把 pip 一律用 `venv/bin/python -m pip` 调用；真装不上时也会直接告诉你要执行哪条命令。

**服务器需要走代理才能出网**
在 `/etc/systemd/system/t00ls-sign.service` 的 `[Service]` 段加一行，然后重新加载：

```ini
Environment=HTTPS_PROXY=http://代理地址:端口
```

```bash
sudo systemctl daemon-reload && sudo systemctl start t00ls-sign.service
```

## 十、卸载

```bash
sudo ./deploy.sh --uninstall
```

会停用并删除 systemd 单元 / cron 任务，并可选删除 `/opt/t00ls-sign`（含账号配置和补签状态文件）。

## 十一、免责声明

仅用于自动化**自己账号**的每日签到，接口由 T00ls 官方开放并鼓励第三方使用。请保持低频（本脚本每天最多 3 次、且已签到即退出），不要改成高频轮询或用于批量账号，以免给站点造成负担并导致账号受限。

自动补签会按站点规则消耗你账号里的 TuBi（每次 20），默认关闭，开启与金额判断权在你；脚本已尽量保守（真漏签才补、每天最多一次、先记账再请求、结果含 TuBi 变化），但请自行留意 `--check` 输出与通知里的 TuBi 数字。

## 十二、许可协议

本项目以 [MIT License](LICENSE) 开源，可自由使用、修改与分发。
