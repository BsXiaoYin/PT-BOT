#!/usr/bin/env python3
"""Telegram bot：在多个 PT 站搜索种子，推送到 qBittorrent 并实时显示下载进度。"""
import asyncio
import hashlib
import html
import logging
import os
import re
import time

import httpx
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # 避免把带 token 的请求 URL 打进日志
log = logging.getLogger("ptbot")

# ---------------- 配置：全部从环境变量读取 ----------------
TG_TOKEN = os.environ["TG_BOT_TOKEN"]
ALLOWED_USERS = {int(x) for x in os.environ["TG_ALLOWED_USERS"].split(",") if x.strip()}
PROXY = os.environ.get("TG_PROXY") or None

# NexusPHP 站点（不用就留空）
PT_NAME = os.environ.get("PT_NAME", "PT站")
PT_BASE = os.environ.get("PT_BASE_URL", "").rstrip("/")
PT_COOKIE = os.environ.get("PT_COOKIE", "")
PT_PASSKEY = os.environ.get("PT_PASSKEY", "")

# 馒头（不用就留空）
MT_TOKEN = os.environ.get("MT_TOKEN", "")
MT_API_BASE = os.environ.get("MT_API_BASE", "https://api.m-team.cc").rstrip("/")

QB_URL = os.environ["QB_URL"].rstrip("/")
QB_USER = os.environ["QB_USER"]
QB_PASS = os.environ["QB_PASS"]
QB_TAGS = os.environ.get("QB_TAGS", "tg-bot")

# 格式：名称=路径;名称=路径    路径是 qB 那台机器（或 qB 容器内）的路径
SAVE_PATHS = [
    (name.strip(), path.strip())
    for name, path in (p.split("=", 1) for p in os.environ["SAVE_PATHS"].split(";") if "=" in p)
]
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "10"))
PROGRESS_INTERVAL = int(os.environ.get("PROGRESS_INTERVAL", "10"))  # 进度刷新间隔（秒）
WATCH_HOURS = float(os.environ.get("WATCH_HOURS", "24"))            # 最长跟踪时间（小时）

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SIZE_RE = re.compile(r"^[\d.,]+\s*[KMGTP]i?B$", re.I)

STATE_NAMES = {
    "downloading": "⬇️ 下载中", "forcedDL": "⬇️ 强制下载", "stalledDL": "⏳ 等待连接",
    "metaDL": "⏳ 获取元数据", "queuedDL": "⏳ 排队中", "allocating": "⏳ 分配空间",
    "checkingDL": "🔍 校验中", "checkingUP": "🔍 校验中", "checkingResumeData": "🔍 校验中",
    "pausedDL": "⏸ 已暂停", "stoppedDL": "⏸ 已暂停", "moving": "📦 移动中",
    "uploading": "✅ 做种中", "stalledUP": "✅ 做种中", "forcedUP": "✅ 做种中",
    "queuedUP": "✅ 排队做种", "pausedUP": "✅ 已完成", "stoppedUP": "✅ 已完成",
    "error": "❌ 出错", "missingFiles": "❌ 文件丢失",
}


# ---------------- 工具函数 ----------------
def _bencode_end(data: bytes, i: int) -> int:
    """返回从位置 i 开始的 bencode 元素的结束位置。"""
    c = data[i:i + 1]
    if c == b"i":
        return data.index(b"e", i) + 1
    if c in (b"l", b"d"):
        i += 1
        while data[i:i + 1] != b"e":
            i = _bencode_end(data, i)
        return i + 1
    colon = data.index(b":", i)
    return colon + 1 + int(data[i:colon])


def info_hash(torrent: bytes) -> str:
    """计算种子的 v1 info-hash，也就是 qB 里显示的 hash。"""
    i = 1  # 跳过最外层字典的 'd'
    while torrent[i:i + 1] != b"e":
        key_end = _bencode_end(torrent, i)
        key = torrent[torrent.index(b":", i) + 1:key_end]
        value_end = _bencode_end(torrent, key_end)
        if key == b"info":
            return hashlib.sha1(torrent[key_end:value_end]).hexdigest()
        i = value_end
    raise ValueError("种子文件里没有 info 字段")


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}"
        n /= 1024


def fmt_eta(seconds: int) -> str:
    if seconds < 0 or seconds >= 8640000:  # qB 用 8640000 表示无穷大
        return "未知"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def render_progress(t: dict) -> str:
    p = t["progress"]
    filled = int(p * 10)
    bar = "█" * filled + "░" * (10 - filled)
    lines = [
        STATE_NAMES.get(t["state"], t["state"]),
        t["name"],
        f"[{bar}] {p * 100:.1f}%",
        f"{fmt_size(t['size'] * p)} / {fmt_size(t['size'])}",
    ]
    if p < 1:
        lines.append(f"速度 {fmt_size(t['dlspeed'])}/s | 剩余 {fmt_eta(t['eta'])}")
        lines.append(f"做种 {t['num_seeds']} | 下载者 {t['num_leechs']}")
    else:
        lines.append(f"上传 {fmt_size(t['upspeed'])}/s | 分享率 {t['ratio']:.2f}")
    lines.append(f"位置 {t['save_path']}")
    return "\n".join(lines)


# ---------------- PT 站：NexusPHP ----------------
def parse_torrents(page: str) -> list[dict]:
    """解析 NexusPHP 的 torrents.php 列表页。"""
    soup = BeautifulSoup(page, "html.parser")
    table = soup.select_one("table.torrents")
    if table is None:
        return []

    results, seen = [], set()
    for tr in table.find_all("tr"):
        # 只处理主表格的行，跳过标题单元格里嵌套的小表格
        if tr.find_parent("table") is not table:
            continue
        link = tr.select_one('a[href*="details.php?id="]')
        if not link:
            continue  # 表头行
        m = re.search(r"id=(\d+)", link["href"])
        if not m or m.group(1) in seen:
            continue
        tid = m.group(1)
        seen.add(tid)

        title = link.get("title") or link.get_text(strip=True)
        tds = tr.find_all("td", recursive=False)

        size = "?"
        for td in tds:
            text = td.get_text(" ", strip=True)
            if SIZE_RE.match(text):
                size = text
                break

        # 做种数 > 0 时是指向 #seeders 的链接；为 0 时是普通文本，默认在第 6 列
        a = tr.select_one('a[href*="#seeders"]')
        if a:
            seed_text = a.get_text(strip=True)
        elif len(tds) > 5:
            seed_text = tds[5].get_text(strip=True)
        else:
            seed_text = "0"
        seeders = int(re.sub(r"\D", "", seed_text) or 0)

        results.append({"id": tid, "title": title, "size": size, "seeders": seeders})

    # 站点已经按做种数排过序，这里再排一次，防止置顶种子打乱顺序
    results.sort(key=lambda x: x["seeders"], reverse=True)
    return results


class NexusPHP:
    def __init__(self, name: str, base: str, cookie: str, passkey: str = ""):
        self.name = name
        self.passkey = passkey
        self.http = httpx.AsyncClient(
            base_url=base,
            headers={"User-Agent": UA, "Cookie": cookie},
            timeout=20,
            follow_redirects=True,
        )

    async def search(self, keyword: str) -> list[dict]:
        r = await self.http.get("/torrents.php", params={
            "search": keyword,
            "search_area": 0,   # 0 = 搜索标题
            "search_mode": 0,   # 0 = 包含全部关键词
            "sort": 7,          # 7 = 按做种数
            "type": "desc",     # 降序
        })
        r.raise_for_status()
        if "login.php" in str(r.url):
            raise RuntimeError(f"{self.name} 的 cookie 已失效，请更新 PT_COOKIE")
        return parse_torrents(r.text)

    async def download(self, tid: str) -> bytes:
        params = {"id": tid}
        if self.passkey:
            params["passkey"] = self.passkey
        r = await self.http.get("/download.php", params=params)
        r.raise_for_status()
        # .torrent 是 bencode 格式，以 'd' 开头；返回 HTML 说明没登录或没有权限
        if not r.content.startswith(b"d"):
            raise RuntimeError("下载到的不是种子文件，可能是 cookie 失效或没有下载权限")
        return r.content


# ---------------- PT 站：馒头（官方 API） ----------------
class MTeam:
    def __init__(self, name: str, token: str, base: str):
        self.name = name
        # 不带 User-Agent 会被网关 302 拦截
        self.http = httpx.AsyncClient(
            base_url=base,
            headers={"x-api-key": token, "User-Agent": UA},
            timeout=20,
        )

    async def _post(self, path: str, **kwargs):
        r = await self.http.post(path, **kwargs)
        if r.is_redirect:
            raise RuntimeError(f"{self.name} API 返回跳转（{r.status_code}），可能是令牌失效或被风控")
        r.raise_for_status()
        j = r.json()
        if str(j.get("code")) != "0":
            raise RuntimeError(f"{self.name} API 报错：{j.get('message')}")
        return j.get("data")

    async def search(self, keyword: str) -> list[dict]:
        # 接口默认按发布时间排序，这里多取一些，再在本地按做种数排序
        data = await self._post("/api/torrent/search", json={
            "mode": "normal",   # normal = 综合区，成人区要用 adult 单独搜
            "keyword": keyword,
            "pageNumber": 1,
            "pageSize": 100,
        })
        results = []
        for t in (data or {}).get("data") or []:
            status = t.get("status") or {}
            results.append({
                "id": str(t["id"]),
                "title": t.get("name") or "",
                "desc": t.get("smallDescr") or "",
                "size": fmt_size(int(t.get("size") or 0)),
                "seeders": int(status.get("seeders") or 0),
            })
        results.sort(key=lambda x: x["seeders"], reverse=True)
        return results

    async def download(self, tid: str) -> bytes:
        url = await self._post("/api/torrent/genDlToken", data={"id": tid})
        if not isinstance(url, str) or not url.startswith("http"):
            raise RuntimeError("没有拿到种子下载链接")
        # 用不带令牌的客户端下载，避免把 x-api-key 发给下载链接所在的域名
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": UA}) as c:
            r = await c.get(url)
        r.raise_for_status()
        if not r.content.startswith(b"d"):
            raise RuntimeError("下载到的不是种子文件")
        return r.content


# ---------------- 站点列表：配置了哪个站就启用哪个 ----------------
SITES: dict = {}

# NexusPHP 站点：PT_ 开头的是第一个站，PT2_ ~ PT9_ 是新增的站，编号可以跳着用
for n in range(1, 10):
    prefix = "PT_" if n == 1 else f"PT{n}_"
    base = os.environ.get(f"{prefix}BASE_URL", "").rstrip("/")
    cookie = os.environ.get(f"{prefix}COOKIE", "")
    if base and cookie:
        key = "np" if n == 1 else f"np{n}"
        SITES[key] = NexusPHP(
            os.environ.get(f"{prefix}NAME", f"PT站{n}"),
            base,
            cookie,
            os.environ.get(f"{prefix}PASSKEY", ""),
        )

if MT_TOKEN:
    SITES["mt"] = MTeam("馒头", MT_TOKEN, MT_API_BASE)

if not SITES:
    raise SystemExit("没有配置任何 PT 站：请在 .env 里填写 PT_BASE_URL/PT_COOKIE 或 MT_TOKEN")

# ---------------- qBittorrent ----------------
class QBittorrent:
    def __init__(self):
        # qB 有 CSRF 校验，Referer 要和 WebUI 地址一致
        self.http = httpx.AsyncClient(base_url=QB_URL, headers={"Referer": QB_URL}, timeout=30)

    async def login(self):
        r = await self.http.post("/api/v2/auth/login",
                                 data={"username": QB_USER, "password": QB_PASS})
        # 旧版成功返回 200 "Ok."，新版返回 204；密码错误返回 "Fails."，IP 被封返回 403
        if not r.is_success or "Fails" in r.text:
            raise RuntimeError(f"qB 登录失败（{r.status_code}）：{r.text[:100]}")

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """发送请求；未登录或会话过期时自动登录并重试一次。"""
        for attempt in range(2):
            r = await self.http.request(method, path, **kwargs)
            if r.status_code in (401, 403) and attempt == 0:
                await self.login()
                continue
            return r

    async def add_torrent(self, content: bytes, filename: str, save_path: str):
        r = await self._request(
            "POST", "/api/v2/torrents/add",
            files={"torrents": (filename, content, "application/x-bittorrent")},
            data={
                "savepath": save_path,
                "autoTMM": "false",  # 必须关闭自动管理，否则 savepath 会被分类路径覆盖
                "tags": QB_TAGS,
            },
        )
        if not r.is_success or "Fails" in r.text:
            raise RuntimeError(f"qB 添加失败（{r.status_code}）：{r.text[:100]}")

    async def torrent_info(self, ih: str) -> dict | None:
        r = await self._request("GET", "/api/v2/torrents/info", params={"hashes": ih})
        r.raise_for_status()
        items = r.json()
        return items[0] if items else None


qb = QBittorrent()


# ---------------- 进度跟踪 ----------------
async def watch_progress(bot, chat_id: int, msg_id: int, ih: str, title: str, notify: bool):
    """定时查询 qB，编辑同一条消息显示进度，直到下载完成、种子被删除或超时。"""
    deadline = time.monotonic() + WATCH_HOURS * 3600
    last_text, misses = None, 0
    await asyncio.sleep(3)  # qB 添加种子是异步的，稍等一下再查

    try:
        while True:
            try:
                t = await qb.torrent_info(ih)
            except Exception as e:
                log.warning("查询进度失败 %s: %s", ih, e)
                await asyncio.sleep(PROGRESS_INTERVAL)
                continue

            done = False
            if t is None:
                misses += 1
                if misses < 5:
                    await asyncio.sleep(PROGRESS_INTERVAL)
                    continue
                text = f"种子已从 qBittorrent 删除，停止跟踪\n{title}"
                done = True
            else:
                misses = 0
                text = render_progress(t)
                if t["progress"] >= 1:
                    done = True
                elif time.monotonic() > deadline:
                    text += f"\n\n已跟踪 {WATCH_HOURS:g} 小时，停止刷新"
                    done = True

            if text != last_text:
                try:
                    await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id)
                    last_text = text
                except RetryAfter as e:  # 编辑太频繁被 Telegram 限流
                    await asyncio.sleep(e.retry_after)
                    continue
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        log.warning("停止跟踪 %s：%s", ih, e)  # 例如消息被删除
                        return

            if done:
                if notify and t is not None and t["progress"] >= 1:
                    await bot.send_message(chat_id, f"✅ 下载完成\n{t['name']}")
                return
            await asyncio.sleep(PROGRESS_INTERVAL)
    except Exception:
        log.exception("进度跟踪异常 %s", ih)


# ---------------- Telegram 处理函数 ----------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    names = "、".join(s.name for s in SITES.values())
    await update.message.reply_text(
        f"用法：/s 关键词\n已启用站点：{names}\n结果按做种数从高到低排列，点编号选择种子。")


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyword = " ".join(ctx.args).strip()
    if not keyword:
        await update.message.reply_text("用法：/s 关键词")
        return

    if len(SITES) == 1:  # 只有一个站点时跳过选择
        msg = await update.message.reply_text(f"正在搜索：{keyword}")
        await run_search(msg, next(iter(SITES)), keyword, ctx)
        return

    kb = [[InlineKeyboardButton(site.name, callback_data=f"s:{key}")
           for key, site in SITES.items()]]
    msg = await update.message.reply_text(f"在哪个站搜索：{keyword}",
                                          reply_markup=InlineKeyboardMarkup(kb))
    # 按钮的 callback_data 最多 64 字节，关键词按消息 ID 存起来
    ctx.user_data.setdefault("keywords", {})[msg.message_id] = keyword


async def run_search(msg, key: str, keyword: str, ctx: ContextTypes.DEFAULT_TYPE):
    site = SITES[key]
    await msg.edit_text(f"正在 {site.name} 搜索：{keyword}")
    try:
        results = (await site.search(keyword))[:MAX_RESULTS]
    except Exception as e:
        log.exception("search failed")
        await msg.edit_text(f"{site.name} 搜索失败：{e}")
        return

    if not results:
        await msg.edit_text(f"{site.name} 没有找到：{keyword}")
        return

    ctx.user_data.setdefault("titles", {}).update(
        {f"{key}:{r['id']}": r["title"] for r in results})

    lines = [f"<b>{html.escape(site.name)}</b> · <b>{html.escape(keyword)}</b>"
             f" 的搜索结果（按做种数排序）：\n"]
    for i, r in enumerate(results, 1):
        line = f"{i}. {html.escape(r['title'][:80])}"
        if r.get("desc"):
            line += f"\n     {html.escape(r['desc'][:40])}"
        line += f"\n     大小 {r['size']} | 做种 {r['seeders']}"
        lines.append(line)
    buttons = [InlineKeyboardButton(str(i), callback_data=f"t:{key}:{r['id']}")
               for i, r in enumerate(results, 1)]
    keyboard = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]

    await msg.edit_text("\n".join(lines), parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if update.effective_user.id not in ALLOWED_USERS:
        await q.answer("无权限", show_alert=True)
        return
    await q.answer()
    data = q.data

    if data == "cancel":
        await q.edit_message_text("已取消")
        return

    if data.startswith("s:"):  # 选中站点 -> 搜索
        key = data[2:]
        keyword = ctx.user_data.get("keywords", {}).pop(q.message.message_id, None)
        if keyword is None or key not in SITES:
            await q.edit_message_text("搜索已过期，请重新发送 /s 关键词")
            return
        await run_search(q.message, key, keyword, ctx)
        return

    parts = data.split(":")

    if parts[0] == "t" and len(parts) == 3:  # 选中种子 -> 选择下载位置
        _, key, tid = parts
        title = ctx.user_data.get("titles", {}).get(f"{key}:{tid}", f"ID {tid}")
        kb = [[InlineKeyboardButton(f"{name}  ({path})", callback_data=f"p:{key}:{tid}:{i}")]
              for i, (name, path) in enumerate(SAVE_PATHS)]
        kb.append([InlineKeyboardButton("取消", callback_data="cancel")])
        # 发新消息，保留搜索结果列表
        await q.message.reply_text(f"已选择：{title}\n请选择下载位置：",
                                   reply_markup=InlineKeyboardMarkup(kb))
        return

    if parts[0] == "p" and len(parts) == 4:  # 选中路径 -> 下载种子、推送到 qB、开始跟踪进度
        _, key, tid, idx = parts
        site = SITES.get(key)
        if site is None:
            await q.edit_message_text("这个站点已经不在配置里了，请重新搜索")
            return
        name, path = SAVE_PATHS[int(idx)]
        title = ctx.user_data.get("titles", {}).get(f"{key}:{tid}", f"ID {tid}")
        await q.edit_message_text(f"正在添加：{title}\n位置：{path}")
        try:
            content = await site.download(tid)
            ih = info_hash(content)
            existing = await qb.torrent_info(ih)
            if existing is None:
                await qb.add_torrent(content, f"{key}-{tid}.torrent", path)
                head = f"已添加到 qBittorrent\n{title}\n位置：{name}（{path}）"
            else:
                head = f"qB 里已有这个种子，不重复添加\n{title}"
        except Exception as e:
            log.exception("add failed")
            await q.edit_message_text(f"添加失败：{title}\n{e}")
            return

        await q.edit_message_text(f"{head}\n\n正在获取进度…")
        notify = existing is None or existing["progress"] < 1  # 本来就下完的种子不再提醒
        ctx.application.create_task(
            watch_progress(ctx.bot, q.message.chat_id, q.message.message_id, ih, title, notify)
        )
        return

    await q.edit_message_text("按钮已过期，请重新搜索")  # 升级前发出的旧格式按钮


def main():
    builder = Application.builder().token(TG_TOKEN)
    if PROXY:
        builder = builder.proxy(PROXY).get_updates_proxy(PROXY)
    app = builder.build()

    only_me = filters.User(user_id=ALLOWED_USERS)
    app.add_handler(CommandHandler("start", cmd_start, filters=only_me))
    app.add_handler(CommandHandler("s", cmd_search, filters=only_me))
    app.add_handler(CallbackQueryHandler(on_button))

    log.info("bot started, sites: %s", ", ".join(s.name for s in SITES.values()))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
