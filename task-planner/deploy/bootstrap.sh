#!/usr/bin/env bash
# Cairn 服务器初始化脚本（Debian 12 / Ubuntu 24.04）
#
#   sudo bash deploy/bootstrap.sh
#
# 做四件事：装 uv 与 Python 3.11、创建运行用户、装依赖、注册 systemd 服务。
# 幂等 —— 重复执行不会破坏已有配置。
#
# 前置：代码已经放在 /opt/cairn/task-planner（git clone 或 rsync 均可）。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/cairn/task-planner}"
APP_USER="${APP_USER:-cairn}"
# auto = 系统 Python 够新就用系统的，否则让 uv 下载 3.11。
# 显式指定（如 PY_VERSION=3.12）则按指定的来。
PY_VERSION="${PY_VERSION:-auto}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "请用 root 运行：sudo bash deploy/bootstrap.sh" >&2
  exit 1
fi

if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
  echo "找不到 $APP_DIR/pyproject.toml —— 先把代码传上去，或用 APP_DIR=... 指定路径" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
say "1/6 检查 Python"
# ---------------------------------------------------------------------------
# 项目要求 >=3.11。Debian 12 自带 3.11，Ubuntu 22.04 只有 3.10 ——
# 那种情况交给 uv 自己下载一份，不去动系统 Python。
if command -v python3 >/dev/null 2>&1; then
  echo "系统 Python: $(python3 -V)"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "安装 uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "uv: $(uv --version)"

# ---------------------------------------------------------------------------
say "2/6 创建运行用户"
# ---------------------------------------------------------------------------
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
  echo "已创建用户 $APP_USER"
else
  echo "用户 $APP_USER 已存在"
fi

# ---------------------------------------------------------------------------
say "3/6 安装依赖"
# ---------------------------------------------------------------------------
cd "$APP_DIR"

# Ubuntu 22.04 自带 Python 3.10，而项目要求 3.11+；24.04 自带 3.12，够用。
# 够新就直接用系统的（省一次下载、少一层权限问题），不够新才让 uv 装。
NEED_UV_PYTHON=0
if [[ "$PY_VERSION" == "auto" ]]; then
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    echo "系统 Python $PY_VERSION 满足 >=3.11，直接使用"
  else
    PY_VERSION="3.11"
    NEED_UV_PYTHON=1
    echo "系统 Python 低于 3.11，改用 uv 下载 $PY_VERSION"
  fi
else
  # 显式指定的版本可能系统里没有，交给 uv 兜底
  NEED_UV_PYTHON=1
fi

# ⚠️ 这个目录必须显式指定。uv 默认装到 $HOME/.local/share/uv/python，
#    而本脚本以 root 运行、HOME=/root（权限 700）—— 服务以 cairn 用户
#    启动时读不到解释器，systemd 直接 exec 失败，报错只有一句
#    status=203/EXEC，很难联想到是权限问题。
UV_PYTHON_INSTALL_DIR="$(dirname "$APP_DIR")/.python"
export UV_PYTHON_INSTALL_DIR

if [[ "$NEED_UV_PYTHON" == "1" ]]; then
  uv python install "$PY_VERSION"
  chmod -R a+rX "$UV_PYTHON_INSTALL_DIR"
fi

# 服务器不需要 pytest / ruff / mypy，跳过 dev 组省 100MB 左右
uv sync --no-dev --python "$PY_VERSION"

# 自检：用**实际运行身份**验证解释器可执行。
# 能提前抓到「root 装、cairn 读不到」这类权限问题，
# 否则要等 systemctl start 才暴露。
if sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -V >/dev/null 2>&1; then
  echo "✓ $APP_USER 可执行 .venv/bin/python（$(sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -V 2>&1)）"
else
  echo "⚠️  $APP_USER 无法执行 .venv/bin/python，尝试修复权限…"
  chmod -R a+rX "$UV_PYTHON_INSTALL_DIR" "$APP_DIR/.venv" 2>/dev/null || true
fi

# 运行期需要可写的两个目录
mkdir -p logs outputs
chown -R "$APP_USER:$APP_USER" "$APP_DIR/logs" "$APP_DIR/outputs"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env" 2>/dev/null || true

# ---------------------------------------------------------------------------
say "4/6 修正 .env 换行"
# ---------------------------------------------------------------------------
# 在 Windows 上编辑的 .env 是 CRLF。systemd 的 EnvironmentFile 不会剥掉 \r，
# 于是 key 变成 "sk-xxx\r" 发给上游 → 401，而且报错信息完全看不出原因。
if [[ -f "$APP_DIR/.env" ]]; then
  if grep -q $'\r' "$APP_DIR/.env"; then
    sed -i 's/\r$//' "$APP_DIR/.env"
    echo "已把 .env 从 CRLF 转成 LF"
  else
    echo ".env 换行正常"
  fi
  chmod 600 "$APP_DIR/.env"

  # 没有令牌 = 接口完全开放。在 127.0.0.1 上无所谓，
  # 但配置了 Nginx 之后就等于把余额和历史记录一起交出去。
  if ! grep -qE '^WEB_ACCESS_TOKEN=.+' "$APP_DIR/.env"; then
    echo "⚠️  WEB_ACCESS_TOKEN 为空 —— 接口没有任何鉴权"
    echo "    生成一个：openssl rand -base64 24"
    echo "    写进 .env 后执行：systemctl restart cairn"
  fi
else
  echo "⚠️  没有 $APP_DIR/.env —— 服务会因缺少 DEEPSEEK_API_KEY 启动失败"
  echo "    cp .env.example .env && 编辑后重跑本脚本"
fi

# ---------------------------------------------------------------------------
say "5/6 生成高德代理片段"
# ---------------------------------------------------------------------------
# 安全密钥不能写进 nginx.conf —— 那个文件要进 git。这里从 .env 读出来，
# 生成一个权限 600 的 snippet，由 nginx.conf 用 include 引进去。
#
# 原理：前端只知道往 /_AMapService 转发，密钥在 Nginx 这一侧拼进上游请求，
# 浏览器拿不到。这是高德官方推荐的生产做法。
AMAP_CONF=/etc/nginx/snippets/amap-proxy.conf
mkdir -p /etc/nginx/snippets

SECRET=""
if [[ -f "$APP_DIR/.env" ]]; then
  SECRET="$(grep -E '^AMAP_JS_SECURITY_CODE=' "$APP_DIR/.env" | head -1 \
    | cut -d= -f2- | tr -d '\r' | xargs || true)"
fi

# 密钥会被原样插进 nginx 配置，含引号或分号会破坏语法。
# 高德的安全密钥是字母数字，出现别的字符说明 .env 写错了。
if [[ -n "$SECRET" && ! "$SECRET" =~ ^[A-Za-z0-9]+$ ]]; then
  echo "⚠️  安全密钥含非字母数字字符，跳过生成（检查 .env 里的 AMAP_JS_SECURITY_CODE）"
  SECRET=""
fi

if [[ -n "$SECRET" ]]; then
  cat > "$AMAP_CONF" <<NGINX
# 由 deploy/bootstrap.sh 生成，不要手工编辑。
# 改密钥请改 .env 里的 AMAP_JS_SECURITY_CODE，然后重跑 bootstrap.sh。
location /_AMapService/v4/map/styles {
    set \$args "\$args&jscode=${SECRET}";
    proxy_pass https://webapi.amap.com/v4/map/styles;
}
location /_AMapService/ {
    set \$args "\$args&jscode=${SECRET}";
    proxy_pass https://restapi.amap.com/;
}
NGINX
  chmod 600 "$AMAP_CONF"
  echo "已生成 $AMAP_CONF（含密钥，权限 600）"

  if ! grep -qE '^AMAP_SERVICE_HOST=.+' "$APP_DIR/.env"; then
    echo "⚠️  .env 里 AMAP_SERVICE_HOST 为空 —— 后端仍会把密钥明文下发"
    echo "    部署到公网请设：AMAP_SERVICE_HOST=/_AMapService"
  fi
else
  printf '# 未配置 AMAP_JS_SECURITY_CODE，地图走前端明文模式\n' > "$AMAP_CONF"
  chmod 644 "$AMAP_CONF"
  echo "未配置安全密钥，跳过（地图仍可用，但密钥会明文下发）"
fi

# ---------------------------------------------------------------------------
say "6/6 注册 systemd 服务"
# ---------------------------------------------------------------------------
install -m 644 "$APP_DIR/deploy/cairn.service" /etc/systemd/system/cairn.service
systemctl daemon-reload
systemctl enable --now cairn

sleep 2
systemctl --no-pager --lines=12 status cairn || true

cat <<EOF

--------------------------------------------------------------------
下一步：配置 Nginx 与证书

  sudo apt install -y nginx certbot python3-certbot-nginx
  sudo cp $APP_DIR/deploy/nginx.conf /etc/nginx/sites-available/cairn
  sudo sed -i 's/example.com/你的域名/g' /etc/nginx/sites-available/cairn
  sudo ln -sf /etc/nginx/sites-available/cairn /etc/nginx/sites-enabled/
  sudo rm -f /etc/nginx/sites-enabled/default
  sudo nginx -t && sudo systemctl reload nginx
  sudo certbot --nginx -d 你的域名

验证：
  curl -s http://127.0.0.1:8000/api/health
  curl -sI https://你的域名/ | head -5

日志：
  journalctl -u cairn -f
--------------------------------------------------------------------
EOF
