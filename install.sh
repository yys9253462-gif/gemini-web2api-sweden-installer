#!/usr/bin/env bash
# Gemini Web2API — Debian 12 Chinese interactive installer
set -Eeuo pipefail

UPSTREAM='https://github.com/Sophomoresty/gemini-web2api.git'
PANEL_REPO='https://github.com/yys9253462-gif/gemini-web2api-sweden-installer.git'
APP_DIR='/opt/gemini-web2api'
PANEL_DIR='/opt/gemini-web2api-panel'
SERVICE_HOME='/var/lib/gemini-web2api'
CONFIG_DIR='/etc/gemini-web2api'
CONFIG_FILE="$CONFIG_DIR/config.json"
PANEL_CONFIG_FILE="$CONFIG_DIR/panel.json"
UNIT_FILE='/etc/systemd/system/gemini-web2api.service'
PANEL_UNIT_FILE='/etc/systemd/system/gemini-web2api-panel.service'
CADDY_FILE='/etc/caddy/Caddyfile'
SERVICE='gemini-web2api'
PORT=8081
PANEL_PORT=8090
DOMAIN=''
PANEL_DOMAIN=''
CADDY_DOMAIN=''
SKIP_PANEL_DOMAIN=0
PANEL_PASSWORD=''
MODE='menu'
INTERACTIVE=1

usage() {
  cat <<'EOF'
Gemini Web2API Debian 12 中文安装器

用法：
  sudo bash install.sh                         启动中文交互菜单
  sudo bash install.sh --dry-run               交互式预演
  sudo bash install.sh --status                交互式查看状态
  sudo bash install.sh --non-interactive [选项] 自动化安装

选项：
  --domain 域名       绑定域名并配置现有 Caddy
  --port 端口         服务端口，默认 8081
  --dry-run           预演，不安装服务、不更改 Caddy
  --status            查看服务状态
  --non-interactive   不显示菜单；需用 --domain/--port 提供参数
  -h, --help          显示此说明
EOF
}

say() { printf '%s\n' "$*"; }
fail() { say "错误：$*" >&2; exit 1; }

read_tty() {
  local prompt="$1" result_name="$2" typed_answer
  if [[ ! -r /dev/tty ]]; then fail '当前没有可交互终端；请连接终端，或使用 --non-interactive。'; fi
  IFS= read -r -p "$prompt" typed_answer </dev/tty || fail '读取交互输入失败。'
  printf -v "$result_name" '%s' "$typed_answer"
}

read_default() {
  local prompt="$1" default_value="$2" result_name="$3" input_value
  read_tty "$prompt [默认：$default_value]：" input_value
  [[ -n "$input_value" ]] || input_value="$default_value"
  printf -v "$result_name" '%s' "$input_value"
}

valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1024 && 10#$1 <= 65535 )); }
valid_domain() {
  [[ ${#1} -le 253 && "$1" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]
}

patch_argon_model() {
  python3 - "$APP_DIR/gemini_web2api/models.py" "$1" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
action = sys.argv[2]
if not path.is_file():
    raise SystemExit(f'上游模型映射文件不存在：{path}')
text = path.read_text(encoding='utf-8')
needle = 'MODELS = {\n'
block = '''    # >>> gemini-web2api-sweden-installer: Gemini 4 Argon alias >>>
    "gemini-4-argon": {
        "mode": 3, "think": 4,
        "desc": "Gemini 4 Argon (Gemini Web Pro category)",
    },
    # <<< gemini-web2api-sweden-installer: Gemini 4 Argon alias <<<
'''
if action == 'remove':
    if 'gemini-web2api-sweden-installer: Gemini 4 Argon alias' in text:
        if block not in text:
            raise SystemExit('Gemini 4 Argon 本地补丁内容发生变化；为保护文件，未自动移除。')
        path.write_text(text.replace(block, '', 1), encoding='utf-8')
elif action == 'apply':
    if 'gemini-web2api-sweden-installer: Gemini 4 Argon alias' in text:
        if block not in text:
            raise SystemExit('Gemini 4 Argon 本地补丁内容异常。')
    elif '"gemini-4-argon"' not in text:
        if text.count(needle) != 1:
            raise SystemExit('上游 MODELS 映射结构与预期不符；未注入别名。')
        path.write_text(text.replace(needle, needle + block, 1), encoding='utf-8')
else:
    raise SystemExit('patch_argon_model 仅支持 remove/apply')
PY
}

managed_caddy_domain() {
  [[ -f "$CADDY_FILE" ]] || return 0
  python3 - "$CADDY_FILE" <<'PY'
import pathlib,re,sys
text=pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
start="# >>> gemini-web2api managed >>>"
end="# <<< gemini-web2api managed <<<"
if start not in text or end not in text: raise SystemExit(0)
block=text.split(start,1)[1].split(end,1)[0]
for line in block.splitlines():
    s=line.strip()
    if not s or s.startswith("#"): continue
    m=re.fullmatch(r"([A-Za-z0-9.-]+)\s*\{",s)
    if m: print(m.group(1)); raise SystemExit(0)
raise SystemExit(2)
PY
}

prompt_port() {
  local answer
  while true; do
    read_default '请输入服务端口（1024-65535）' "$PORT" answer
    if valid_port "$answer"; then PORT=$((10#$answer)); return; fi
    say '端口无效，请输入 1024 到 65535 之间的整数。'
  done
}

prompt_domain() {
  local choice answer default_choice=2
  local existing_domain=''
  if [[ -z "$DOMAIN" ]] && [[ -f "$CADDY_FILE" ]] && grep -Fq '# >>> gemini-web2api managed >>>' "$CADDY_FILE"; then
    existing_domain="$(managed_caddy_domain || true)"
    [[ -z "$existing_domain" ]] || DOMAIN="$existing_domain"
  fi
  [[ -n "$DOMAIN" ]] && default_choice=1
  while true; do
    read_default '是否通过域名公开管理面板？输入 1=公开面板，2=面板仅本机/SSH 隧道' "$default_choice" choice
    case "$choice" in
      1)
        while true; do
          read_default '请输入已解析到本服务器的域名' "$DOMAIN" answer
          if valid_domain "$answer"; then DOMAIN="$answer"; SKIP_PANEL_DOMAIN=0; return; fi
          say '域名格式不正确，请重新输入。'
        done
        ;;
      2) DOMAIN=''; SKIP_PANEL_DOMAIN=1; return ;;
      *) say '选项无效，请输入 1 或 2。' ;;
    esac
  done
}

parse_args() {
  while (($#)); do
    case "$1" in
      --help|-h) usage; exit 0 ;;
      --dry-run) MODE='dry-run' ;;
      --status) MODE='status' ;;
      --non-interactive)
        INTERACTIVE=0
        if [[ "$MODE" == menu ]]; then MODE='install'; fi
        ;;
      --domain)
        (($# >= 2)) || fail '--domain 后缺少域名。'
        DOMAIN="$2"; shift
        ;;
      --port)
        (($# >= 2)) || fail '--port 后缺少端口。'
        PORT="$2"; shift
        ;;
      *) fail "无法识别的参数：$1（使用 --help 查看用法）" ;;
    esac
    shift
  done
  return 0
}

interactive_menu() {
  if [[ "$INTERACTIVE" -ne 1 || "$MODE" != menu ]]; then return 0; fi
  say '================================================'
  say '       Gemini Web2API Debian 12 一键安装器'
  say '================================================'
  say '1. 安装 / 更新'
  say '2. 预演（不安装、不改配置）'
  say '3. 查看安装状态'
  say '0. 退出'
  local choice
  while true; do
    read_tty '请选择操作：' choice
    case "$choice" in
      1) MODE='install'; return ;;
      2) MODE='dry-run'; return ;;
      3) MODE='status'; return ;;
      0) say '已退出。'; exit 0 ;;
      *) say '选项无效，请输入 0、1、2 或 3。' ;;
    esac
  done
}

require_debian12() {
  [[ -r /etc/os-release ]] || fail '无法读取系统版本。'
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == debian && "${VERSION_ID:-}" == 12 ]] || fail "仅支持 Debian 12；当前系统为 ${PRETTY_NAME:-未知系统}。"
}

ensure_root_install() {
  (( EUID == 0 )) && return 0
  command -v sudo >/dev/null 2>&1 || fail '请使用 root 运行，或先安装 sudo。'
  say '需要管理员权限，正在请求 sudo……'
  local args=(--non-interactive --port "$PORT")
  [[ -n "$DOMAIN" ]] && args+=(--domain "$DOMAIN")
  exec sudo -E bash "$SCRIPT_PATH" "${args[@]}"
}

status_mode() {
  local active='' port="$PORT" panel_active
  say '=== 服务状态 ==='
  if [[ ! -f "$UNIT_FILE" ]]; then
    say '服务状态：尚未安装'
  else
    active="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    case "$active" in
      active) say '服务状态：运行中' ;;
      inactive) say '服务状态：已停止' ;;
      failed) say '服务状态：启动失败' ;;
      *) say '服务状态：状态未知' ;;
    esac
  fi
  if [[ ! -f "$PANEL_UNIT_FILE" ]]; then
    say '管理面板：尚未安装'
  else
    panel_active="$(systemctl is-active gemini-web2api-panel.service 2>/dev/null || true)"
    [[ "$panel_active" == active ]] && say '管理面板：运行中' || say "管理面板：${panel_active:-状态未知}"
  fi
  if curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$PANEL_PORT/api/session" 2>/dev/null; then
    say "面板接口：正常（127.0.0.1:$PANEL_PORT）"
  else
    say '面板接口：未连通'
  fi
  [[ -f "$CONFIG_FILE" ]] && port="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$CONFIG_FILE" 2>/dev/null || echo "$PORT")"
  if curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$port/" 2>/dev/null; then
    say '本机健康接口：正常'
  else
    say '本机健康接口：未连通'
  fi
  if ss -ltn 2>/dev/null | grep -Fq "127.0.0.1:$port"; then
    say "监听地址：127.0.0.1:$port（仅本机）"
  else
    say '监听地址：未发现预期端口'
  fi
  if [[ -f "$CADDY_FILE" ]] && command -v caddy >/dev/null 2>&1; then
    if caddy validate --config "$CADDY_FILE" >/dev/null 2>&1; then say 'Caddy 配置：有效'; else say 'Caddy 配置：校验失败'; fi
  else
    say 'Caddy 配置：未安装或找不到配置文件'
  fi
}

preflight() {
  valid_port "$PORT" || fail '端口必须是 1024 到 65535 之间的整数。'
  PANEL_DOMAIN=''
  CADDY_DOMAIN=''
  local existing_managed_domain=''
  if [[ -f "$CADDY_FILE" ]] && grep -Fq '# >>> gemini-web2api managed >>>' "$CADDY_FILE"; then
    existing_managed_domain="$(managed_caddy_domain)" || fail '无法识别现有的 Caddy 管理站点；请检查 Caddyfile。'
    [[ -n "$existing_managed_domain" ]] || fail '现有 Caddy 管理站点缺少域名配置。'
    if [[ -n "$DOMAIN" && "$DOMAIN" != "$existing_managed_domain" ]]; then
      fail "当前 Caddy 管理站点是 $existing_managed_domain；请使用该域名，或先手动迁移站点。"
    fi
    CADDY_DOMAIN="$existing_managed_domain"
    [[ "$SKIP_PANEL_DOMAIN" -eq 1 ]] || PANEL_DOMAIN="$existing_managed_domain"
  elif [[ -n "$DOMAIN" ]]; then
    valid_domain "$DOMAIN" || fail '域名格式不正确。'
    [[ -f "$CADDY_FILE" ]] || fail "选择了域名，但未找到 $CADDY_FILE。"
    if grep -Eq "^[[:space:]]*${DOMAIN//./\.}[[:space:]]*\{" "$CADDY_FILE"; then
      fail "Caddy 中已经存在 $DOMAIN 站点配置；为避免重复，已停止。"
    fi
    CADDY_DOMAIN="$DOMAIN"
    [[ "$SKIP_PANEL_DOMAIN" -eq 1 ]] || PANEL_DOMAIN="$DOMAIN"
  fi
  if [[ -n "$CADDY_DOMAIN" ]]; then
    valid_domain "$CADDY_DOMAIN" || fail '现有 Caddy 域名格式无效。'
    command -v caddy >/dev/null 2>&1 || fail '未找到 Caddy 命令。'
    systemctl is-active --quiet caddy || fail '现有 Caddy 服务未运行。'
    getent ahostsv4 "$CADDY_DOMAIN" >/dev/null 2>&1 || fail "域名 $CADDY_DOMAIN 没有可解析的 IPv4 记录；请先配置 DNS。"
  fi
  if ss -ltn 2>/dev/null | awk -v suffix=":$PORT" 'index($4,suffix)>0 {found=1} END {exit !found}' && [[ ! -f "$UNIT_FILE" ]]; then
    fail "端口 $PORT 已被其他服务占用，请选择其他端口。"
  fi
  if ss -ltn 2>/dev/null | awk -v suffix=":$PANEL_PORT" 'index($4,suffix)>0 {found=1} END {exit !found}' && [[ ! -f "$PANEL_UNIT_FILE" ]]; then
    fail "面板端口 $PANEL_PORT 已被其他服务占用。"
  fi
  if [[ -e "$APP_DIR" ]] && [[ ! -d "$APP_DIR/.git" ]]; then
    local old_home='' app_owner=''
    if id "$SERVICE" >/dev/null 2>&1; then old_home="$(getent passwd "$SERVICE" | cut -d: -f6)"; fi
    app_owner="$(stat -c '%U' "$APP_DIR" 2>/dev/null || true)"
    [[ "$old_home" == "$APP_DIR" || "$app_owner" == "$SERVICE" ]] || fail "$APP_DIR 已存在且不是目标 Git 仓库或本安装器的服务账户目录；为保护原文件，未覆盖。"
  fi
}

prepare_panel_source() {
  if [[ -d "$PANEL_DIR/.git" ]]; then
    [[ "$(git -C "$PANEL_DIR" remote get-url origin)" == "$PANEL_REPO" ]] || fail '面板目录来源不符，已停止且未覆盖。'
    [[ "$(git -C "$PANEL_DIR" branch --show-current)" == main ]] || fail '面板仓库不在 main 分支，已停止且未切换分支。'
    [[ -z "$(git -C "$PANEL_DIR" status --porcelain)" ]] || fail '面板目录有未提交更改，已停止且未覆盖。'
    git -C "$PANEL_DIR" fetch -q origin main
    git -C "$PANEL_DIR" merge-base --is-ancestor HEAD FETCH_HEAD || fail '面板仓库不是安全快进更新，已停止且未覆盖。'
    git -C "$PANEL_DIR" merge --quiet --ff-only FETCH_HEAD
  elif [[ -e "$PANEL_DIR" ]] && [[ -n "$(find "$PANEL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    fail "$PANEL_DIR 已存在非空目录且不是面板仓库；为保护原文件，未覆盖。"
  else
    git clone -q --depth=1 --branch main "$PANEL_REPO" "$PANEL_DIR"
  fi
  chown -R root:root "$PANEL_DIR"
  chmod -R go-w "$PANEL_DIR"
}

prepare_panel_config() {
  if [[ ! -f "$PANEL_CONFIG_FILE" ]]; then
    PANEL_PASSWORD="$(python3 - "$PANEL_CONFIG_FILE" "$PANEL_DOMAIN" <<'PY'
import base64,hashlib,json,os,secrets,sys
path,domain=sys.argv[1],sys.argv[2]
password=secrets.token_urlsafe(24)
salt=secrets.token_bytes(16)
digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,310000)
cfg={'salt':base64.b64encode(salt).decode(),'password_hash':base64.b64encode(digest).decode(),'public_domain':domain}
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f: json.dump(cfg,f,indent=2); f.write('\n')
print(password)
PY
    )"
  else
    python3 - "$PANEL_CONFIG_FILE" "$PANEL_DOMAIN" <<'PY'
import json,os,sys,tempfile
p,domain=sys.argv[1],sys.argv[2]
c=json.load(open(p))
if not c.get('salt') or not c.get('password_hash'):
    raise SystemExit('面板密码配置无效；为避免覆盖，请先检查 panel.json。')
c['public_domain']=domain
fd,tmp=tempfile.mkstemp(prefix='.panel.',dir=os.path.dirname(p))
with os.fdopen(fd,'w') as f: json.dump(c,f,indent=2); f.write('\n')
os.chmod(tmp,0o600); os.replace(tmp,p)
PY
  fi
  chown root:root "$PANEL_CONFIG_FILE"
  chmod 0600 "$PANEL_CONFIG_FILE"
}

write_managed_caddy_block() {
  python3 - "$CADDY_FILE" "$CADDY_DOMAIN" "$PANEL_DOMAIN" "$effective_port" "$PANEL_PORT" <<'PY'
import os,pathlib,re,stat,sys,tempfile
path=pathlib.Path(sys.argv[1]); domain,panel_domain,api_port,panel_port=sys.argv[2],sys.argv[3],sys.argv[4],sys.argv[5]
start='# >>> gemini-web2api managed >>>'; end='# <<< gemini-web2api managed <<<'
text=path.read_text(encoding='utf-8')
panel_route=''
if panel_domain:
    panel_route=f'''        @panel_root path /panel
        redir @panel_root /panel/ 308
        handle_path /panel/* {{
            reverse_proxy 127.0.0.1:{panel_port}
        }}
'''
block=f'''{start}
{domain} {{
    encode zstd gzip
    route {{
{panel_route}        @native_api path /v1beta/*
        respond @native_api "此服务暂不通过公网开放 Gemini 原生接口" 404
        reverse_proxy 127.0.0.1:{api_port}
    }}
}}
{end}'''
if start in text or end in text:
    if text.count(start)!=1 or text.count(end)!=1: raise SystemExit('Caddy 管理标记重复或不匹配。')
    a=text.index(start); b=text.index(end,a)+len(end)
    old=text[a:b]
    m=re.search(r'^([A-Za-z0-9.-]+)\s*\{',old,re.M)
    if not m or m.group(1)!=domain: raise SystemExit('现有 Caddy 管理站点域名与本次选择不一致。')
    text=text[:a]+block+text[b:]
else:
    if re.search(r'^\s*'+re.escape(domain)+r'\s*\{',text,re.M|re.I):
        raise SystemExit('Caddy 已存在同名域名站点，未修改。')
    text=text.rstrip()+'\n\n'+block+'\n'
st=path.stat()
fd,tmp=tempfile.mkstemp(prefix='.Caddyfile.gemini.',dir=str(path.parent))
with os.fdopen(fd,'w',encoding='utf-8') as f: f.write(text); f.flush(); os.fsync(f.fileno())
os.chmod(tmp,stat.S_IMODE(st.st_mode)); os.chown(tmp,st.st_uid,st.st_gid); os.replace(tmp,path)
PY
}

install_or_update() {
  local old_home recovery_dir
  say '【1/8】检查并安装系统依赖……'
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git python3 python3-venv python3-pip curl openssl ca-certificates iproute2

  say '【2/8】准备服务账户和目录……'
  if id "$SERVICE" >/dev/null 2>&1; then
    old_home="$(getent passwd "$SERVICE" | cut -d: -f6)"
    if [[ "$old_home" == "$APP_DIR" ]] && [[ ! -d "$APP_DIR/.git" ]] && [[ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
      install -d -o "$SERVICE" -g "$SERVICE" -m 0750 "$SERVICE_HOME"
      recovery_dir="$SERVICE_HOME/recovered-old-home-$(date +%Y%m%d%H%M%S)"
      [[ ! -e "$recovery_dir" ]] || fail "恢复目录已存在：$recovery_dir；请重试稍后安装。"
      mv "$APP_DIR" "$recovery_dir"
      usermod --home "$SERVICE_HOME" "$SERVICE"
      say "发现上次失败留下的服务账户目录，已保留在 $recovery_dir；继续安装。"
    elif [[ -e "$APP_DIR" ]] && [[ ! -d "$APP_DIR/.git" ]] && [[ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
      fail "$APP_DIR 已有非仓库文件，不属于旧服务账户主目录；为保护原文件，未覆盖。"
    elif [[ "$old_home" != "$SERVICE_HOME" ]]; then
      install -d -o "$SERVICE" -g "$SERVICE" -m 0750 "$SERVICE_HOME"
      usermod --home "$SERVICE_HOME" "$SERVICE"
    fi
  else
    if [[ -e "$APP_DIR" ]] && [[ ! -d "$APP_DIR/.git" ]] && [[ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
      fail "$APP_DIR 已有文件且不是目标 Git 仓库；为保护原文件，未覆盖。"
    fi
    useradd --system --user-group --home-dir "$SERVICE_HOME" --create-home --shell /usr/sbin/nologin "$SERVICE"
  fi
  install -d -o "$SERVICE" -g "$SERVICE" -m 0750 "$APP_DIR"
  install -d -o root -g "$SERVICE" -m 0750 "$CONFIG_DIR"

  say '【3/8】下载或安全更新上游项目……'
  if [[ -d "$APP_DIR/.git" ]]; then
    [[ "$(sudo -u "$SERVICE" git -C "$APP_DIR" remote get-url origin)" == "$UPSTREAM" ]] || fail '安装目录中的仓库来源不符，未覆盖现有文件。'
    [[ "$(sudo -u "$SERVICE" git -C "$APP_DIR" branch --show-current)" == main ]] || fail '上游仓库当前不在 main 分支，未切换分支。'
    patch_argon_model remove || fail '无法安全撤销上一轮 Gemini 4 Argon 兼容补丁。'
    [[ -z "$(sudo -u "$SERVICE" git -C "$APP_DIR" status --porcelain)" ]] || fail '上游目录有未提交更改，未覆盖现有文件。'
    sudo -u "$SERVICE" git -C "$APP_DIR" fetch -q origin main
    sudo -u "$SERVICE" git -C "$APP_DIR" merge-base --is-ancestor HEAD FETCH_HEAD || fail '上游不是本地代码的快进更新，未覆盖现有文件。'
    sudo -u "$SERVICE" git -C "$APP_DIR" merge --quiet --ff-only FETCH_HEAD
  elif [[ -e "$APP_DIR" ]] && [[ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    fail "$APP_DIR 已存在非空目录且不是目标 Git 仓库，未覆盖。"
  else
    sudo -u "$SERVICE" git clone -q --depth=1 --branch main "$UPSTREAM" "$APP_DIR"
  fi
  patch_argon_model apply || fail '无法为上游安全添加 Gemini 4 Argon 模型别名。'
  install -d -o "$SERVICE" -g "$SERVICE" -m 0750 "$APP_DIR/.cache"
  grep -qxF '/.venv/' "$APP_DIR/.git/info/exclude" 2>/dev/null || printf '/.venv/\n' >> "$APP_DIR/.git/info/exclude"
  chown -R "$SERVICE:$SERVICE" "$APP_DIR"

  say '【4/8】创建安全配置并安装 Python 依赖……'
  if [[ ! -f "$CONFIG_FILE" ]]; then
    local api_key
    api_key="$(openssl rand -hex 32)"
    python3 - "$CONFIG_FILE" "$PORT" "$api_key" <<'PY'
import json,sys
path,port,key=sys.argv[1],int(sys.argv[2]),sys.argv[3]
cfg={"host":"127.0.0.1","port":port,"retry_attempts":3,"retry_delay_sec":2,
     "request_timeout_sec":180,"default_model":"gemini-4-argon",
     "auth_user":None,"xsrf_token":None,"log_requests":True,
     "cookie_file":None,"proxy":None,"api_keys":[key],"temporary_chats":True}
with open(path,"w") as f: json.dump(cfg,f,indent=2); f.write("\n")
PY
    chown root:"$SERVICE" "$CONFIG_FILE"
    chmod 0640 "$CONFIG_FILE"
  else
    python3 - "$CONFIG_FILE" <<'PY'
import json,os,shutil,sys,tempfile
from pathlib import Path
from datetime import datetime
c=json.load(open(sys.argv[1]))
if c.get('host') != '127.0.0.1': raise SystemExit('现有配置不是仅本机监听；为避免公网暴露，已停止。')
keys=c.get('api_keys')
if not isinstance(keys,list) or not keys or not all(isinstance(k,str) and k for k in keys):
    raise SystemExit('现有配置没有有效 api_keys；为避免免密访问，已停止。')
if not isinstance(c.get('port'),int) or not (1024 <= c['port'] <= 65535): raise SystemExit('现有配置端口无效。')
if c.get('default_model') in ('gemini-3.6-flash','gemini-3.7-flash',None,''):
    path=Path(sys.argv[1]); backup=path.with_name(path.name+'.bak-'+datetime.now().strftime('%Y%m%d%H%M%S'))
    shutil.copy2(path,backup)
    c['default_model']='gemini-4-argon'
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=str(path.parent))
    with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(c,f,ensure_ascii=False,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.chmod(tmp,0o640); os.replace(tmp,path)
PY
    chown root:"$SERVICE" "$CONFIG_FILE"
    chmod 0640 "$CONFIG_FILE"
  fi
  sudo -u "$SERVICE" python3 -m venv "$APP_DIR/.venv"
  sudo -u "$SERVICE" "$APP_DIR/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
  local effective_port
  effective_port="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$CONFIG_FILE")"

  say '【5/8】创建并启动 API 服务……'
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Gemini 网页转 OpenAI 兼容 API 服务
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE
Group=$SERVICE
WorkingDirectory=$APP_DIR
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$APP_DIR/.venv/bin/python -m gemini_web2api --config $CONFIG_FILE
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=$APP_DIR $CONFIG_DIR
RestrictSUIDSGID=true
LockPersonality=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "$UNIT_FILE"
  systemctl daemon-reload
  systemctl enable "$SERVICE.service"
  systemctl restart "$SERVICE.service"
  sleep 2
  systemctl is-active --quiet "$SERVICE.service" || {
    say '错误：服务启动失败，下面是系统诊断日志：'
    journalctl -u "$SERVICE.service" -n 80 --no-pager || true
    fail '请检查日志后再重试。'
  }

  say '【6/8】安装管理面板……'
  prepare_panel_source
  prepare_panel_config
  if [[ -n "$PANEL_PASSWORD" ]]; then
    say "管理面板初始密码（请立即保存）：$PANEL_PASSWORD"
  fi
  cat > "$PANEL_UNIT_FILE" <<EOF_PANEL_UNIT
[Unit]
Description=Gemini Web2API 管理面板
After=network-online.target gemini-web2api.service
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$PANEL_DIR/panel
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $PANEL_DIR/panel/server.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$CONFIG_DIR
RestrictSUIDSGID=true
LockPersonality=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF_PANEL_UNIT
  chmod 0644 "$PANEL_UNIT_FILE"
  systemctl daemon-reload
  systemctl enable gemini-web2api-panel.service
  systemctl restart gemini-web2api-panel.service
  sleep 1
  systemctl is-active --quiet gemini-web2api-panel.service || {
    say '错误：管理面板启动失败，以下为诊断日志：'
    journalctl -u gemini-web2api-panel.service -n 80 --no-pager || true
    fail '请检查面板日志后重试。'
  }

  say '【7/8】配置域名反代和 HTTPS……'
  if [[ -n "$CADDY_DOMAIN" ]]; then
    local dns_ips backup
    dns_ips="$(getent ahostsv4 "$CADDY_DOMAIN" | awk '{print $1}' | sort -u | tr '\n' ' ')"
    say "域名解析结果：$dns_ips（请确认最终指向本服务器）"
    backup="$CADDY_FILE.bak-gemini-web2api-$(date +%Y%m%d-%H%M%S)"
    cp -a "$CADDY_FILE" "$backup"
    if ! write_managed_caddy_block; then
      cp -a "$backup" "$CADDY_FILE"
      fail '生成 Caddy 站点配置失败，已恢复原配置。'
    fi
    if ! caddy validate --config "$CADDY_FILE" >/dev/null 2>&1; then
      cp -a "$backup" "$CADDY_FILE"
      fail 'Caddy 配置校验失败，已恢复原配置。'
    fi
    say 'Caddy 配置校验：通过。'
    if ! systemctl reload caddy; then
      cp -a "$backup" "$CADDY_FILE"
      caddy validate --config "$CADDY_FILE" >/dev/null 2>&1 && systemctl reload caddy || true
      fail 'Caddy 热加载失败，已恢复原配置。'
    fi
  else
    say '未配置域名；面板和 API 仅能通过本机或 SSH 隧道访问。'
  fi

  say '【8/8】验证服务、面板和 API 鉴权……'
  local_port="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$CONFIG_FILE")"
  curl -fsS -o /dev/null --max-time 10 "http://127.0.0.1:$local_port/" || fail '本机健康接口未通过。'
  ss -ltn | grep -Fq "127.0.0.1:$local_port" || fail '服务未按预期仅监听本机地址。'
  python3 - "$CONFIG_FILE" <<'PY'
import json,sys,urllib.request,urllib.error
c=json.load(open(sys.argv[1])); url='http://127.0.0.1:%d/v1/models'%c['port']
try:
    urllib.request.urlopen(url,timeout=5)
    raise SystemExit('错误：无密钥请求未被拒绝。')
except urllib.error.HTTPError as e:
    if e.code != 401: raise SystemExit('错误：无密钥请求未返回 401。')
req=urllib.request.Request(url,headers={'Authorization':'Bearer '+c['api_keys'][0]})
with urllib.request.urlopen(req,timeout=5) as r: data=json.load(r)
if r.status != 200 or not data.get('data'): raise SystemExit('错误：密钥请求或模型列表验证失败。')
print('API 鉴权验证：无密钥=401；正确密钥=200；可用模型数=%d'%len(data['data']))
PY
  say '服务状态：运行中'
  say "监听地址：127.0.0.1:$local_port"
  curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$PANEL_PORT/api/session" || fail '本机管理面板接口未通过。'
  say "管理面板：运行于 127.0.0.1:$PANEL_PORT"
  if [[ -n "$CADDY_DOMAIN" ]]; then
    local code='' attempt
    say '正在检查 HTTPS（首次申请证书可能需要一些时间）……'
    for attempt in $(seq 1 12); do
      code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "https://$CADDY_DOMAIN/" 2>/dev/null || true)"
      [[ "$code" =~ ^2[0-9][0-9]$ ]] && break
      sleep 5
    done
    [[ "$code" =~ ^2[0-9][0-9]$ ]] && say "HTTPS 检查：正常（HTTP $code）" || say 'HTTPS 证书暂未就绪；稍后查看：journalctl -u caddy -n 100'
  fi
  python3 - "$CONFIG_FILE" "$DOMAIN" <<'PY'
import json,sys
c=json.load(open(sys.argv[1]))
print('接口密钥（请立即保存）：'+c['api_keys'][0])
PY
if [[ -n "$PANEL_DOMAIN" ]]; then
  say "管理面板：https://$PANEL_DOMAIN/panel/"
else
  say '管理面板：本机 http://127.0.0.1:8090/（可用 SSH 隧道转发 8090）'
fi
python3 - "$CONFIG_FILE" "$CADDY_DOMAIN" <<'PY'
import json,sys
c=json.load(open(sys.argv[1]))
print('接口地址：'+('https://'+sys.argv[2]+'/v1' if sys.argv[2] else 'http://127.0.0.1:'+str(c['port'])+'/v1（仅本机或 SSH 隧道）'))
PY
}

main() {
  SCRIPT_PATH="$(readlink -f "$0")"
  parse_args "$@"

  require_debian12
  interactive_menu
  if [[ "$INTERACTIVE" -eq 1 && "$MODE" != status ]]; then
    prompt_port
    prompt_domain
  fi

  if [[ "$MODE" == status ]]; then status_mode; return; fi
  preflight
  if [[ "$MODE" == dry-run ]]; then
    say "【预演】系统：${PRETTY_NAME:-Debian 12}；安装目录：$APP_DIR；API 域名：${CADDY_DOMAIN:-不绑定}；面板域名：${PANEL_DOMAIN:-仅本机/SSH 隧道}"
    say '计划：安装 API 和管理面板；创建受限服务账户、随机 API Key 与随机面板密码。'
    [[ -z "$CADDY_DOMAIN" ]] || say '计划：备份并更新现有 Caddy 配置，反代 API/面板并阻止公网访问未鉴权的 Gemini 原生接口。'
    say '预演完成；没有安装软件或更改服务器配置。'
    return
  fi

  if [[ "$INTERACTIVE" -eq 1 ]]; then
    say ''
    say "目标系统：${PRETTY_NAME:-Debian 12}"
    say "服务端口：$PORT"
    if [[ -n "$CADDY_DOMAIN" ]]; then say "API 地址：https://$CADDY_DOMAIN/v1"; else say 'API 访问方式：仅本机 / SSH 隧道'; fi
    if [[ -n "$PANEL_DOMAIN" ]]; then say "管理面板：https://$PANEL_DOMAIN/panel/"; else say '管理面板：仅本机 / SSH 隧道'; fi
    local confirm
    read_tty '确认开始安装/更新？输入 Y 继续，其他输入取消：' confirm
    [[ "${confirm,,}" == y ]] || { say '已取消，未执行安装。'; return; }
  fi
  ensure_root_install
  install_or_update
}

main "$@"
