## 功能

- `/s 关键词` 搜索 PT 站，结果按做种数从高到低排列
- 点击编号选择种子，再选择预设的下载位置
- bot 下载 `.torrent` 文件并上传到 qBittorrent，同时指定保存路径
- 在同一条消息里实时刷新下载进度、速度和剩余时间，下载完成后另发一条通知
- qB 里已有相同种子时不重复添加，直接显示进度
- 只响应白名单里的 Telegram 用户

## 使用方法

| 命令 | 说明 |
|---|---|
| `/start` | 显示用法 |
| `/s 关键词` | 搜索种子 |

流程：发送 `/s 关键词` → 点击编号 → 选择下载位置 → 等待进度更新。

## 准备工作

1. **创建 bot**：在 Telegram 里找 `@BotFather`，发送 `/newbot`，记下 token。
2. **获取自己的 user ID**：给 `@userinfobot` 发一条消息，它会回复你的数字 ID。
3. **获取 PT 站 cookie**：浏览器登录 PT 站，按 F12 打开开发者工具，切到“网络（Network）”，刷新种子页，点开 `torrents.php` 请求，复制请求头里 `Cookie:` 后面的整串内容。
4. **qBittorrent**：开启 WebUI，记下地址、用户名和密码。

## 配置

复制 `.env.example` 为 `.env`，然后填写：

| 变量 | 必填 | 说明 |
|---|---|---|
| `TG_BOT_TOKEN` | 是 | BotFather 给的 token |
| `TG_ALLOWED_USERS` | 是 | 允许使用 bot 的 user ID，多个用英文逗号分隔 |
| `TG_PROXY` | 否 | 服务器访问不了 Telegram 时填写，如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:1080` |
| `PT_BASE_URL` | 是 | PT 站地址，如 `https://pt.example.com` |
| `PT_COOKIE` | 是 | PT 站 cookie |
| `PT_PASSKEY` | 否 | 下载种子时需要 passkey 的站点填写 |
| `QB_URL` | 是 | qB WebUI 地址。bot 和 qB 在同一台机器上时填 `http://127.0.0.1:8080` |
| `QB_USER` / `QB_PASS` | 是 | qB WebUI 用户名和密码 |
| `QB_TAGS` | 否 | 添加种子时打的标签，默认 `tg-bot` |
| `SAVE_PATHS` | 是 | 下载位置，格式 `名称=路径;名称=路径` |
| `MAX_RESULTS` | 否 | 搜索结果最多显示几条，默认 10 |
| `PROGRESS_INTERVAL` | 否 | 进度刷新间隔（秒），默认 10，不要低于 5 |
| `WATCH_HOURS` | 否 | 单个种子最长跟踪时间（小时），默认 24 |

`SAVE_PATHS` 里的路径要用 qB 视角下的路径。qB 跑在 Docker 里时，写容器内的路径（如 `/downloads/movies`），并且 qB 要对这些目录有写权限。

## 部署（Debian）

以下命令用 root 执行。

### 1. 安装依赖

```bash
apt update
apt install -y python3 python3-venv git curl nano
```

### 2. 安装 Python 依赖并填写配置

```bash
cd /opt/ptbot
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env
```

### 3. 手动试运行

```bash
cd /opt/ptbot
set -a; . ./.env; set +a
venv/bin/python bot.py
```

看到 `bot started` 后，在 Telegram 里发 `/s 关键词`，把整个流程走一遍。确认没问题后按 `Ctrl+C` 停止。

### 4. 配置 systemd 常驻运行

```bash
useradd -r -s /usr/sbin/nologin ptbot
chown -R ptbot:ptbot /opt/ptbot
chmod 600 /opt/ptbot/.env

cat > /etc/systemd/system/ptbot.service <<'EOF'
[Unit]
Description=PT Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
User=ptbot
WorkingDirectory=/opt/ptbot
EnvironmentFile=/opt/ptbot/.env
ExecStart=/opt/ptbot/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ptbot
systemctl status ptbot
```

状态显示 `active (running)` 就表示部署成功。bot 会开机自启，崩溃后 5 秒自动重启。

## 日常运维

### 服务管理

```bash
systemctl status ptbot        # 查看状态
systemctl restart ptbot       # 重启
systemctl stop ptbot          # 停止
journalctl -u ptbot -f        # 实时查看日志
journalctl -u ptbot -n 100    # 查看最近 100 行日志
```

### 更新代码

```bash
cd /opt/ptbot
git pull
venv/bin/pip install -r requirements.txt   # requirements.txt 有变化时才需要
chown -R ptbot:ptbot /opt/ptbot
systemctl restart ptbot
```

### 修改配置

```bash
nano /opt/ptbot/.env
systemctl restart ptbot
```
