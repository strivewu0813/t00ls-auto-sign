#!/usr/bin/env bash
# =============================================================================
# T00ls 自动签到 —— Ubuntu 一键部署脚本
#
#   sudo ./deploy.sh              交互式安装（创建 venv + systemd timer）
#   sudo ./deploy.sh --update     只更新程序文件（不碰配置和定时器），升级脚本后用它
#   sudo ./deploy.sh --cron       不用 systemd，改装 crontab 定时任务
#   sudo ./deploy.sh --no-prompt  不交互，直接用环境变量/已有 config.ini
#   sudo ./deploy.sh --test       立即手动跑一次签到
#   sudo ./deploy.sh --status     查看定时器与最近日志
#   sudo ./deploy.sh --uninstall  卸载
#
# 可覆盖的变量（环境变量）：
#   T00LS_INSTALL_DIR    安装目录，默认 /opt/t00ls-sign
#   T00LS_SERVICE_USER   运行身份，默认 root
#   T00LS_SCHEDULE       每天签到时间（北京时间 HH:MM，逗号分隔），如 00:05,00:35
#   T00LS_USERNAME / T00LS_PASSWORD / T00LS_QUESTION_ID / T00LS_QUESTION_ANSWER
#   T00LS_DINGTALK_WEBHOOK / T00LS_AUTO_BU_SIGN
# =============================================================================
set -euo pipefail

# 每天的触发时间（北京时间，逗号分隔，24 小时制）。服务器在哪个时区都不影响，
# systemd 日历表达式里会显式带上 Asia/Shanghai。
DEFAULT_SCHEDULE="09:37,13:07,21:07"
SCHEDULE="${T00LS_SCHEDULE:-}"
APP_NAME="t00ls-sign"
INSTALL_DIR="${T00LS_INSTALL_DIR:-/opt/t00ls-sign}"
SERVICE_USER="${T00LS_SERVICE_USER:-root}"
UNIT_DIR="/etc/systemd/system"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_BIN="${INSTALL_DIR}/venv/bin/python"
PY_VER=""
CONFIG_FILE="${INSTALL_DIR}/config.ini"

MODES=()
for arg in "$@"; do
  case "$arg" in
    --cron|--no-prompt|--test|--status|--uninstall|--update) MODES+=("$arg") ;;
    -h|--help) sed -n '2,/^set -/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "未知参数：$arg（-h 查看帮助）" >&2; exit 2 ;;
  esac
done
has_mode() { local m; for m in "${MODES[@]:-}"; do [[ "$m" == "$1" ]] && return 0; done; return 1; }

c_info()  { printf '\033[32m[+]\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[33m[!]\033[0m %s\n' "$*" >&2; }
c_die()   { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

need_root() { [[ "${EUID}" -eq 0 ]] || c_die "请用 root 运行：sudo $0 ${MODES[*]:-}"; }

# --------------------------------------------------------------------------- #
# 卸载 / 状态 / 测试
# --------------------------------------------------------------------------- #
do_uninstall() {
  need_root
  c_info "停止并移除 systemd 单元"
  systemctl disable --now "${APP_NAME}.timer" 2>/dev/null || true
  systemctl disable --now "${APP_NAME}.service" 2>/dev/null || true
  rm -f "${UNIT_DIR}/${APP_NAME}.timer" "${UNIT_DIR}/${APP_NAME}.service"
  systemctl daemon-reload 2>/dev/null || true
  if crontab -l 2>/dev/null | grep -q "t00ls_sign.py"; then
    c_info "清理 crontab 中的旧任务"
    crontab -l 2>/dev/null | grep -v "t00ls_sign.py" | crontab - || true
  fi
  if [[ -d "${INSTALL_DIR}" ]]; then
    local ans=""
    read -r -p "是否删除 ${INSTALL_DIR}（含账号配置）？[y/N] " ans || true
    if [[ "${ans,,}" == "y" ]]; then
      rm -rf "${INSTALL_DIR}"
      c_info "已删除 ${INSTALL_DIR}"
    fi
  fi
  c_info "卸载完成"
}

do_status() {
  echo "== systemd 定时器 =="
  systemctl list-timers --all "${APP_NAME}.timer" --no-pager 2>/dev/null || true
  echo
  echo "== 最近 30 行日志（journal） =="
  journalctl -u "${APP_NAME}.service" -n 30 --no-pager 2>/dev/null || true
  [[ -f "${INSTALL_DIR}/t00ls-sign.log" ]] && { echo; echo "== 文件日志末尾 =="; tail -n 20 "${INSTALL_DIR}/t00ls-sign.log"; }
  return 0
}

do_test() {
  need_root
  [[ -x "${PY_BIN}" ]] || c_die "未找到 ${PY_BIN}，请先执行安装：sudo $0"
  c_info "立即执行一次签到"
  set +e
  "${PY_BIN}" "${INSTALL_DIR}/t00ls_sign.py" --config "${CONFIG_FILE}" -v
  local rc=$?
  set -e
  if [[ "${rc}" -eq 0 ]]; then c_info "执行成功"; else c_warn "执行失败，退出码 ${rc}（看上面日志定位原因）"; fi
  return "${rc}"
}

do_update() {
  need_root
  [[ -d "${INSTALL_DIR}" ]] || c_die "${INSTALL_DIR} 不存在，请先完整安装：sudo $0"
  ensure_python
  mkdir -p "${INSTALL_DIR}"
  c_info "更新程序文件（配置与定时任务保持不变）"
  install -m 644 "${SRC_DIR}/t00ls_sign.py" "${INSTALL_DIR}/t00ls_sign.py"
  if [[ -f "${SRC_DIR}/requirements.txt" ]]; then
    install -m 644 "${SRC_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
  fi
  prepare_venv
  "${PY_BIN}" -m pip install --quiet -r "${INSTALL_DIR}/requirements.txt"
  c_info "依赖已确认"
  "${PY_BIN}" "${INSTALL_DIR}/t00ls_sign.py" --version \
    || c_die "更新后的脚本无法运行，请检查上面的报错"
  echo
  c_info "更新完成。可用的命令选项："
  "${PY_BIN}" "${INSTALL_DIR}/t00ls_sign.py" --help | sed -n '/^options:/,$p' | sed 's/^/  /'
  echo
  echo "  测试通知： ${PY_BIN} ${INSTALL_DIR}/t00ls_sign.py -c ${CONFIG_FILE} --test-notify"
  echo "  立即签到： sudo $0 --test"
}

# --------------------------------------------------------------------------- #
# 依赖与配置
# --------------------------------------------------------------------------- #
install_venv_pkg() {
  # Ubuntu 的 pip 由 python3-venv 提供；缺它时 python3 -m venv 会成功建目录但不装 pip
  c_warn "python3 缺少 ensurepip（Ubuntu 上由 python3-venv 提供），尝试自动安装…"
  apt-get update -qq || true
  apt-get install -y -qq python3-venv >/dev/null 2>&1
  if [[ -n "${PY_VER}" ]]; then
    apt-get install -y -qq "python${PY_VER}-venv" >/dev/null 2>&1 || true
  fi
  python3 -c 'import ensurepip' >/dev/null 2>&1
}

ensure_python() {
  command -v python3 >/dev/null 2>&1 || c_die "未安装 python3，请先：apt-get update && apt-get install -y python3 python3-venv"
  PY_VER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  c_info "python3 版本：${PY_VER}"
  # 别用 python3 -m venv --help 做探测：缺 python3-venv 的机器上它同样会成功，
  # 结果就是建出一个没有 pip 的虚拟环境（报错：venv/bin/pip: No such file or directory）。
  # 真正要探测的是 ensurepip 能不能用。
  if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    install_venv_pkg || c_die "自动安装 python3-venv 失败。请手动执行：apt-get install -y python3-venv"
  fi
}

prepare_venv() {
  # 上次中断 / 缺 python3-venv 留下的半成品：目录在、python 在，但没有 pip
  if [[ -x "${PY_BIN}" ]] && ! "${PY_BIN}" -m pip --version >/dev/null 2>&1; then
    c_warn "已存在的虚拟环境没有可用的 pip（半成品），将重建：${INSTALL_DIR}/venv"
    rm -rf "${INSTALL_DIR}/venv"
  fi

  if [[ ! -x "${PY_BIN}" ]]; then
    c_info "创建虚拟环境"
    if ! python3 -m venv "${INSTALL_DIR}/venv"; then
      c_warn "python3 -m venv 执行失败，安装 python3-venv 后重试"
      install_venv_pkg || true
      rm -rf "${INSTALL_DIR}/venv"
      python3 -m venv "${INSTALL_DIR}/venv" \
        || c_die "创建虚拟环境失败。请手动执行：apt-get install -y python3-venv && rm -rf ${INSTALL_DIR}/venv && python3 -m venv ${INSTALL_DIR}/venv"
    fi
  fi

  # 兜底：venv 建出来了但里面没有 pip，就用 ensurepip 自己引导
  if ! "${PY_BIN}" -m pip --version >/dev/null 2>&1; then
    c_warn "虚拟环境内没有 pip，尝试用 ensurepip 引导安装"
    "${PY_BIN}" -m ensurepip --upgrade --default-pip >/dev/null 2>&1 || true
  fi
  if ! "${PY_BIN}" -m pip --version >/dev/null 2>&1; then
    c_die "虚拟环境内无法安装 pip。请手动执行：apt-get install -y python3-venv && rm -rf ${INSTALL_DIR}/venv && python3 -m venv ${INSTALL_DIR}/venv"
  fi
  c_info "虚拟环境就绪：$("${PY_BIN}" -m pip --version | awk '{print $1, $2}')"
}

write_config_interactive() {
  if [[ -f "${CONFIG_FILE}" ]] && ! has_mode --no-prompt; then
    local ans=""
    # 注意：read 遇到 EOF（非交互执行 / Ctrl-D）会返回非 0，配合 set -e 会直接中断脚本，
    # 因此所有 read 都要 || true 兜底，保持默认行为而不是静默退出。
    read -r -p "${CONFIG_FILE} 已存在，是否覆盖？[y/N] " ans || true
    [[ "${ans,,}" == "y" ]] || { c_info "保留原有配置"; return 0; }
  fi
  [[ -f "${CONFIG_FILE}" ]] && has_mode --no-prompt && { c_info "沿用已有配置 ${CONFIG_FILE}"; return 0; }

  local username="${T00LS_USERNAME:-}" password="${T00LS_PASSWORD:-}"
  local qid="${T00LS_QUESTION_ID:-}" qans="${T00LS_QUESTION_ANSWER:-}"
  local cookie="${T00LS_COOKIE:-}"
  local hook="${T00LS_DINGTALK_WEBHOOK:-}"
  local bu="${T00LS_AUTO_BU_SIGN:-}"

  if ! has_mode --no-prompt && [[ -z "${username}" ]]; then
    echo
    echo "-------------------------------------------------------------"
    echo " 请输入 T00ls 登录信息（直接回车可跳过，改用 Cookie 方式）"
    echo "-------------------------------------------------------------"
    read -r -p "用户名: " username || true
    if [[ -n "${username}" ]]; then
      read -r -s -p "密码（不回显）: " password || true
      echo
      echo "安全提问编号（0=没有，1-7 见 README）"
      read -r -p "question_id [0]: " qid || true
      qid="${qid:-0}"
      if [[ "${qid}" != "0" ]]; then read -r -p "安全提问答案: " qans || true; fi
    else
      echo "请粘贴浏览器里的 Cookie（F12 -> Network -> Request Headers -> Cookie）"
      read -r -p "Cookie: " cookie || true
    fi
    echo
    echo "-------------------------------------------------------------"
    echo " 钉钉通知（签到成功/失败都会推送，可留空以后再加）"
    echo "-------------------------------------------------------------"
    read -r -p "钉钉机器人 Webhook: " hook || true
    echo
    echo "-------------------------------------------------------------"
    echo " 自动补签：只有检测到漏签时才补，每天最多一次，每次消耗 20 TuBi"
    echo "-------------------------------------------------------------"
    read -r -p "开启自动补签？[y/N] " bu || true
  fi

  local pw_md5="" bu_flag="false" hook_line="" notify_flag="false" hook_value="${hook}"
  [[ -n "${password}" ]] && pw_md5="$(printf '%s' "${password}" | md5sum | awk '{print $1}')"
  if [[ "${bu,,}" == "y" || "${bu,,}" == "yes" || "${bu,,}" == "true" || "${bu}" == "1" ]]; then
    bu_flag="true"
  fi
  if [[ -n "${hook_value}" ]]; then
    notify_flag="true"
  else
    hook_value=""
  fi

  # umask 077 + chmod 双保险，确保含密码 MD5 的配置文件只有属主可读
  (umask 077; cat > "${CONFIG_FILE}" <<EOF
[t00ls]
base_url = https://www.t00ls.com
username = ${username}
password_md5 = ${pw_md5}
question_id = ${qid:-0}
question_answer = ${qans}
cookie = ${cookie}
timeout = 20
retries = 3
log_file = ${INSTALL_DIR}/${APP_NAME}.log
state_file = ${INSTALL_DIR}/${APP_NAME}-state.json
auto_bu_sign = ${bu_flag}
bu_sign_within_days = 3

[notify]
enabled = ${notify_flag}
channels = dingtalk
always = false
dingtalk_webhook = ${hook_value}
wecom_webhook =
serverchan_key =
bark_url =
telegram_token =
telegram_chat_id =
smtp_host =
smtp_port = 465
smtp_ssl = true
smtp_user =
smtp_password =
mail_from =
mail_to =
EOF
  )
  if ! chmod 600 "${CONFIG_FILE}" 2>/dev/null; then
    c_warn "无法将 ${CONFIG_FILE} 权限设为 600，请手动检查：chmod 600 ${CONFIG_FILE}"
  fi
  [[ -n "${pw_md5}" ]] || [[ -n "${cookie}" ]] || c_warn "账号与 Cookie 都为空，请稍后编辑 ${CONFIG_FILE} 再运行"
  c_info "已写入配置 ${CONFIG_FILE}（权限 600）"
}

# --------------------------------------------------------------------------- #
# 签到时间表（按北京时间，服务器时区无关）
# --------------------------------------------------------------------------- #
SCHEDULE_TIMES=()

utc_of_beijing() {
  # 北京时间 HH:MM -> UTC HH:MM。中国不使用夏令时，永远是 UTC+8，所以直接算，
  # 不依赖 date 的时区数据库（精简系统上 tzdata 可能缺 Asia/Shanghai）。
  # 注意 00:05 北京时间 = 前一天 16:05 UTC，但"每天固定 UTC 时刻"与"每天固定
  # 北京时刻"等价，所以按模 24 小时取即可。
  local hh="${1%%:*}" mm="${1##*:}"
  local total
  total=$(( (10#${hh} * 60 + 10#${mm} - 480 + 1440) % 1440 ))
  printf '%02d:%02d' $(( total / 60 )) $(( total % 60 ))
}

load_schedule() {
  local raw="${SCHEDULE:-$DEFAULT_SCHEDULE}"
  local -a items=()
  raw="${raw// /}"
  IFS=',' read -r -a items <<< "${raw}"
  SCHEDULE_TIMES=()
  local item
  for item in "${items[@]}"; do
    [[ -z "${item}" ]] && continue
    [[ "${item}" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] \
      || c_die "签到时间格式不对：${item}（应为 HH:MM 24 小时制，多个用逗号分隔，如 00:05,00:35）"
    SCHEDULE_TIMES+=("${item}")
  done
  [[ ${#SCHEDULE_TIMES[@]} -gt 0 ]] || c_die "没有有效的签到时间：${raw}"
  SCHEDULE="$(IFS=','; echo "${SCHEDULE_TIMES[*]}")"
}

# 生成 systemd 日历表达式：优先带时区后缀，老 systemd 则换算成 UTC
oncalendar_specs() {
  local -a tz_specs=() utc_specs=()
  local t hhmm
  for t in "${SCHEDULE_TIMES[@]}"; do
    tz_specs+=("*-*-* ${t}:00 Asia/Shanghai")
  done
  if systemd-analyze calendar "${tz_specs[0]}" >/dev/null 2>&1; then
    printf '%s\n' "${tz_specs[@]}"
    return 0
  fi

  local ok=1
  for t in "${SCHEDULE_TIMES[@]}"; do
    hhmm="$(utc_of_beijing "$t")"
    if [[ -z "${hhmm}" ]]; then ok=0; break; fi
    utc_specs+=("*-*-* ${hhmm}:00 UTC")
  done
  if [[ "${ok}" -eq 1 ]] && systemd-analyze calendar "${utc_specs[0]}" >/dev/null 2>&1; then
    c_warn "当前 systemd 不支持日历里的时区后缀，已换算成 UTC（北京时间 - 8 小时）" >&2
    printf '%s\n' "${utc_specs[@]}"
    return 0
  fi

  c_warn "无法解析带时区的日历表达式，将按【服务器本地时间】执行，请务必自行核对时区！" >&2
  for t in "${SCHEDULE_TIMES[@]}"; do
    printf '%s\n' "*-*-* ${t}:00"
  done
}

show_schedule() {
  local -a specs=()
  local spec next
  while IFS= read -r spec; do specs+=("${spec}"); done < <(oncalendar_specs)
  c_info "每天触发时间（北京时间）：${SCHEDULE}"
  for spec in "${specs[@]}"; do
    next="$(systemd-analyze calendar "${spec}" 2>/dev/null | grep -m1 -E 'elapse|下次' || true)"
    printf '    %s\n      %s\n' "${spec}" "${next:-（无法解析，请手工检查）}"
  done
}

install_systemd() {
  command -v systemctl >/dev/null 2>&1 || c_die "系统没有 systemd，请改用：sudo $0 --cron"

  local -a specs=()
  local spec timers=""
  while IFS= read -r spec; do specs+=("${spec}"); done < <(oncalendar_specs)
  for spec in "${specs[@]}"; do timers+="OnCalendar=${spec}"$'\n'; done

  cat > "${UNIT_DIR}/${APP_NAME}.service" <<EOF
[Unit]
Description=T00ls 每日自动签到
Documentation=https://www.t00ls.com/api.html
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PY_BIN} ${INSTALL_DIR}/t00ls_sign.py --config ${CONFIG_FILE}
# 失败时记入日志；脚本本身幂等，重复触发不会重复签到
# 900 秒是给"站点尚未换日"时的重试留出空间（默认 3 次 × 120 秒）
TimeoutStartSec=900
EOF

  cat > "${UNIT_DIR}/${APP_NAME}.timer" <<EOF
[Unit]
Description=每天定时执行 T00ls 自动签到

[Timer]
${timers}RandomizedDelaySec=600
Persistent=true
Unit=${APP_NAME}.service

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now "${APP_NAME}.timer" >/dev/null
  c_info "已启用 ${APP_NAME}.timer（${#SCHEDULE_TIMES[@]} 个时间点，脚本幂等，多点触发等于免费重试）"
  show_schedule
  systemctl list-timers "${APP_NAME}.timer" --no-pager | head -3
}

install_cron() {
  command -v crontab >/dev/null 2>&1 || c_die "未安装 crontab，请执行：apt-get install -y cron"
  local current lines="" t hh mm
  for t in "${SCHEDULE_TIMES[@]}"; do
    hh="${t%%:*}"; mm="${t##*:}"
    # 用 10# 前缀避免 08/09 被当成八进制
    lines+="$((10#${mm})) $((10#${hh})) * * * ${PY_BIN} ${INSTALL_DIR}/t00ls_sign.py --config ${CONFIG_FILE} >> ${INSTALL_DIR}/${APP_NAME}.log 2>&1"$'\n'
  done
  current="$(crontab -l 2>/dev/null | grep -v "t00ls_sign.py" || true)"
  {
    echo "# T00ls 自动签到（北京时间每天 ${SCHEDULE}）"
    echo "CRON_TZ=Asia/Shanghai"
    printf '%s' "${lines}"
    [[ -n "${current}" ]] && printf '%s\n' "${current}"
  } | crontab -
  c_info "已写入 crontab（北京时间 ${SCHEDULE}）："
  printf '%s' "${lines}" | sed 's/^/    /'
  c_warn "cron 对时区的支持不统一：若你的 cron 忽略 CRON_TZ，请执行 timedatectl set-timezone Asia/Shanghai"
  c_warn "把服务器时区改成北京时间（这样 cron 与日志时间也都对得上），或按本地时间自行换算"
  c_info "查看：crontab -l"
}

# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
main() {
  if has_mode --uninstall; then do_uninstall; exit 0; fi
  if has_mode --status;    then do_status;    exit 0; fi

  need_root
  if [[ "${SERVICE_USER}" != "root" ]] && ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    c_die "运行身份 ${SERVICE_USER} 不存在。请先创建该用户，或改用 T00LS_SERVICE_USER=root 运行"
  fi
  if has_mode --update; then
    do_update
    exit 0
  fi
  if has_mode --test; then
    set +e
    do_test
    rc=$?
    set -e
    exit "${rc}"
  fi

  echo "=============================================="
  echo " T00ls 自动签到部署（Ubuntu）"
  echo " 安装目录：${INSTALL_DIR}"
  echo " 运行身份：${SERVICE_USER}"
  echo "=============================================="

  ensure_python

  c_info "准备安装目录"
  mkdir -p "${INSTALL_DIR}"
  install -m 644 "${SRC_DIR}/t00ls_sign.py" "${INSTALL_DIR}/t00ls_sign.py"
  [[ -f "${SRC_DIR}/requirements.txt" ]] && install -m 644 "${SRC_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"

  c_info "创建 Python 虚拟环境并安装依赖"
  prepare_venv
  # 统一用 venv 自己的 python 调 pip：直接调 bin/pip 脚本路径会在缺 pip、
  # shebang 不对或 PATH 异常时抛出 "No such file or directory" 这种难以理解的错误。
  "${PY_BIN}" -m pip install --quiet --upgrade pip
  "${PY_BIN}" -m pip install --quiet -r "${INSTALL_DIR}/requirements.txt"
  "${PY_BIN}" -c "import requests" >/dev/null 2>&1 \
    || c_die "依赖安装失败：requests 不可用。请检查网络/代理后重跑，或手动执行：${PY_BIN} -m pip install -r ${INSTALL_DIR}/requirements.txt"

  write_config_interactive

  echo
  echo "-------------------------------------------------------------"
  echo " 每天什么时候签到？按【北京时间】填，服务器在美国也不影响"
  echo " 多个时间用逗号分隔（脚本幂等，多设几个等于免费重试）"
  echo " 例：00:05,00:35 —— 先在北京时间刚过 0 点时签，半小时后再兜底一次"
  echo "-------------------------------------------------------------"
  if ! has_mode --no-prompt && [[ -z "${SCHEDULE}" ]]; then
    read -r -p "时间 [${DEFAULT_SCHEDULE}]: " SCHEDULE || true
  fi
  load_schedule
  c_info "签到时间（北京时间）：${SCHEDULE}"

  chmod 700 "${INSTALL_DIR}" 2>/dev/null || true
  [[ "${SERVICE_USER}" != "root" ]] && chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" 2>/dev/null || true

  if has_mode --cron; then install_cron; else install_systemd; fi

  echo
  c_info "安装完成。常用命令："
  echo "  立即测试一次： sudo $0 --test"
  echo "  只看状态：     ${PY_BIN} ${INSTALL_DIR}/t00ls_sign.py --config ${CONFIG_FILE} --check"
  echo "  查看日志：     journalctl -u ${APP_NAME} -n 50 --no-pager"
  echo "  修改配置：     sudo nano ${CONFIG_FILE}"
  grep -q '^auto_bu_sign = true' "${CONFIG_FILE}" 2>/dev/null && {
    echo
    c_warn "已开启自动补签：检测到漏签时才会补，每天最多一次，每次消耗 20 TuBi"
    echo "  想手动补一次：${PY_BIN} ${INSTALL_DIR}/t00ls_sign.py --config ${CONFIG_FILE} --bu-sign -v"
  }
  grep -q '^dingtalk_webhook = .\+' "${CONFIG_FILE}" 2>/dev/null \
    || c_warn "未配置钉钉 Webhook，签到不会推送通知（可稍后编辑 ${CONFIG_FILE}）"
}

main
