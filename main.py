# main.py
import os, sqlite3, time, secrets, string, threading, csv, tempfile
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, request, redirect, abort
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from zipfile import ZipFile, ZIP_DEFLATED

# ---------- ENV ----------
load_dotenv()
BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID   = os.getenv("CHANNEL_ID")         # @kanal или numeric id
OWNER_ID     = int(os.getenv("OWNER_ID", "0"))
REDIR_HOST   = os.getenv("REDIRECT_HOST", "0.0.0.0")
REDIR_PORT   = int(os.getenv("REDIRECT_PORT", "8080"))
BASE_URL     = os.getenv("BASE_URL", f"http://localhost:{REDIR_PORT}")
ANTI_BURST_S = int(os.getenv("ANTI_BURST_SECONDS", "10"))  # запрет повторных кликов с одного IP за X сек
REPORT_CHAT_ID = os.getenv("REPORT_CHAT_ID", "").strip()   # куда слать отчёты (канал/чат/user), бот должен быть там админом
MAX_EXPORT_MB  = int(os.getenv("MAX_EXPORT_MB", "15"))

# ---------- DB ----------
DB = "store.sqlite3"
conn = sqlite3.connect(DB, check_same_thread=False)
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_msg_id INTEGER,
  created_at INTEGER
);""")
cur.execute("""CREATE TABLE IF NOT EXISTS rates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_msg_id INTEGER,
  item_idx INTEGER,      -- 0 = оценка всей подборки
  user_id INTEGER,
  emoji TEXT,
  sentiment TEXT,        -- pos | neu | neg
  kind TEXT,             -- item | all
  ts INTEGER
);""")
cur.execute("""CREATE TABLE IF NOT EXISTS redirects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT UNIQUE,
  post_msg_id INTEGER,
  item_idx INTEGER,      -- 1..3
  target_url TEXT,
  created_at INTEGER
);""")
cur.execute("""CREATE TABLE IF NOT EXISTS redirect_hits(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  redirect_id INTEGER,
  ts INTEGER,
  ip TEXT,
  ua TEXT,
  referer TEXT
);""")
conn.commit()

# ---------- Sentiment ----------
POS = {"🔥","👍","❤️","👌"}
NEU = {"🤷"}
NEG = {"👎","😡","💩"}

def sentiment_of(emoji:str)->str:
    if emoji in POS: return "pos"
    if emoji in NEG: return "neg"
    return "neu"

# ---------- Redirect service (Flask) ----------
flask_app = Flask(__name__)

def _last_hit_ts(redirect_id:int, ip:str)->int|None:
    c = conn.cursor()
    c.execute("""SELECT ts FROM redirect_hits
                 WHERE redirect_id=? AND ip=?
                 ORDER BY ts DESC LIMIT 1""", (redirect_id, ip))
    row = c.fetchone()
    return row[0] if row else None

@flask_app.get("/r/<token>")
def go(token):
    c = conn.cursor()
    c.execute("SELECT id, target_url FROM redirects WHERE token = ?", (token,))
    row = c.fetchone()
    if not row:
        return abort(404)
    rid, url = row
    now = int(time.time())
    ip = request.remote_addr or ""
    ua = request.headers.get("User-Agent","")
    ref = request.headers.get("Referer","")

    # anti-burst: игнорим запись, если с этого IP был хит < ANTI_BURST_S сек назад
    if ANTI_BURST_S > 0:
        last_ts = _last_hit_ts(rid, ip)
        if last_ts is None or (now - last_ts) >= ANTI_BURST_S:
            c.execute("INSERT INTO redirect_hits(redirect_id, ts, ip, ua, referer) VALUES(?,?,?,?,?)",
                      (rid, now, ip, ua, ref))
            conn.commit()
    else:
        c.execute("INSERT INTO redirect_hits(redirect_id, ts, ip, ua, referer) VALUES(?,?,?,?,?)",
                  (rid, now, ip, ua, ref))
        conn.commit()

    return redirect(url, code=302)

def run_flask():
    flask_app.run(host=REDIR_HOST, port=REDIR_PORT, threaded=True)

def rand_token(n=10):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(n))

def create_redirect(post_msg_id:int, item_idx:int, target_url:str)->str:
    for _ in range(5):
        token = rand_token(10)
        try:
            conn.execute(
                "INSERT INTO redirects(token, post_msg_id, item_idx, target_url, created_at) VALUES(?,?,?,?,?)",
                (token, post_msg_id, item_idx, target_url, int(time.time()))
            )
            conn.commit()
            return f"{BASE_URL}/r/{token}"
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Не удалось сгенерировать токен редиректа")

# ---------- Utils ----------
def _target_report_chat(update: Update):
    # если задан REPORT_CHAT_ID — шлём туда, иначе в чат, откуда пришла команда
    return REPORT_CHAT_ID if REPORT_CHAT_ID else (update.effective_chat.id if update and update.effective_chat else None)

def _zip_if_too_big(csv_path: str, max_mb: int) -> tuple[str, str]:
    """
    Если CSV > max_mb, упаковываем в ZIP.
    Возвращает (path_to_send, display_filename).
    """
    limit = max_mb * 1024 * 1024
    size = os.path.getsize(csv_path)
    if size <= limit:
        return csv_path, os.path.basename(csv_path)

    zip_path = csv_path.replace(".csv", ".zip")
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as z:
        z.write(csv_path, arcname=os.path.basename(csv_path))
    return zip_path, os.path.basename(zip_path)

def parse_date(s:str)->datetime|None:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except:
        return None

# ---------- Telegram part ----------
async def post_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Вставь свои 3 ссылки/заголовка
    items = [
        {"title": "Курс по LLM",        "url": "https://example.com/llm"},
        {"title": "AutoGPT практикум",  "url": "https://example.com/autogpt"},
        {"title": "MLOps пайплайн",     "url": "https://example.com/mlops"},
    ]
    today = time.strftime('%Y-%m-%d')
    lines = [f"🧠 Дайджест ИИ ({today})\n"]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}) {it['title']}\n")  # URL не печатаем — клики считаются редиректом

    msg = await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text="\n".join(lines),
        disable_web_page_preview=True
    )
    conn.execute("INSERT INTO posts(channel_msg_id, created_at) VALUES(?,?)",
                 (msg.message_id, int(time.time())))
    conn.commit()

    kb_rows = []
    for i, it in enumerate(items, 1):
        short_url = create_redirect(msg.message_id, i, it["url"])
        kb_rows.append([
            InlineKeyboardButton(f"Смотреть #{i}", url=short_url),
            InlineKeyboardButton("Оценить", callback_data=f"rate:item:{i}")
        ])
    # Общая оценка
    kb_rows.append([
        InlineKeyboardButton("🔥", callback_data="rate:all:🔥"),
        InlineKeyboardButton("👍", callback_data="rate:all:👍"),
        InlineKeyboardButton("❤️", callback_data="rate:all:❤️"),
        InlineKeyboardButton("👌", callback_data="rate:all:👌"),
    ])
    kb_rows.append([
        InlineKeyboardButton("🤷", callback_data="rate:all:🤷"),
        InlineKeyboardButton("👎", callback_data="rate:all:👎"),
        InlineKeyboardButton("😡", callback_data="rate:all:😡"),
        InlineKeyboardButton("💩", callback_data="rate:all:💩"),
    ])
    markup = InlineKeyboardMarkup(kb_rows)

    await context.bot.edit_message_reply_markup(
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        reply_markup=markup
    )
    if update.effective_message:
        await update.effective_message.reply_text(f"Опубликовано. ID: {msg.message_id}")

async def on_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    parts = (q.data or "").split(":")  # rate:item:2 | rate:all:🔥
    if len(parts) < 3:
        return
    kind = parts[1]
    if kind == "item":
        item_idx = int(parts[2])
        emoji = "★"
        sent = "pos"
    else:
        item_idx = 0
        emoji = parts[2]
        sent = sentiment_of(emoji)

    conn.execute(
        "INSERT INTO rates(post_msg_id,item_idx,user_id,emoji,sentiment,kind,ts) VALUES(?,?,?,?,?,?,?)",
        (q.message.message_id, item_idx, uid, emoji, sent, kind, int(time.time()))
    )
    conn.commit()
    await q.answer("Принято.")

def stats_last_days(days:int):
    since_ts = int((datetime.utcnow() - timedelta(days=days)).timestamp())
    c = conn.cursor()

    c.execute("SELECT channel_msg_id FROM posts WHERE created_at >= ?", (since_ts,))
    post_ids = [r[0] for r in c.fetchall()]
    posts_cnt = len(post_ids)

    clicks_total = 0
    per_post_item = {}
    if post_ids:
        q = f"""
          SELECT r.post_msg_id, r.item_idx, COUNT(h.id)
          FROM redirects r
          LEFT JOIN redirect_hits h ON h.redirect_id = r.id AND h.ts >= ?
          WHERE r.post_msg_id IN ({",".join("?"*len(post_ids))})
          GROUP BY r.post_msg_id, r.item_idx
        """
        c.execute(q, (since_ts, *post_ids))
        for pid, idx, cnt in c.fetchall():
            per_post_item.setdefault(pid, {1:0,2:0,3:0})
            per_post_item[pid][idx] = cnt or 0
            clicks_total += cnt or 0

    per_item_sum = {1:0,2:0,3:0}
    per_item_posts = {1:0,2:0,3:0}
    shares = {1:[],2:[],3:[]}
    for pid, d in per_post_item.items():
        s = sum(d.values())
        for i in (1,2,3):
            per_item_sum[i] += d.get(i,0)
            per_item_posts[i] += 1
            if s > 0:
                shares[i].append(d.get(i,0)/s)

    avg_clicks_per_item = {i: (per_item_sum[i]/per_item_posts[i] if per_item_posts[i] else 0.0) for i in (1,2,3)}
    avg_share_per_item  = {i: (sum(shares[i])/len(shares[i]) if shares[i] else 0.0) for i in (1,2,3)}

    c.execute("""SELECT sentiment, COUNT(*) FROM rates WHERE ts >= ? GROUP BY sentiment""", (since_ts,))
    by_sent = {row[0]: row[1] for row in c.fetchall()}
    pos_cnt = by_sent.get("pos",0); neu_cnt = by_sent.get("neu",0); neg_cnt = by_sent.get("neg",0)

    return {
        "posts": posts_cnt,
        "clicks_total": clicks_total,
        "avg_clicks_per_item": avg_clicks_per_item,
        "avg_share_per_item": avg_share_per_item,
        "sentiments": {"pos":pos_cnt,"neu":neu_cnt,"neg":neg_cnt}
    }

def pct(x): 
    return f"{x*100:.1f}%"

def fmt_stats(days, s):
    lines = [f"📊 За {days} дн."]
    lines.append(f"Постов: {s['posts']}")
    lines.append(f"Кликов «Смотреть» всего: {s['clicks_total']}")
    lines.append("Средние клики/пост по пунктам:")
    for i in (1,2,3):
        lines.append(f"  #{i}: {s['avg_clicks_per_item'][i]:.2f}  | доля в посте ≈ {pct(s['avg_share_per_item'][i])}")
    lines.append(f"Оценки: ✅ {s['sentiments']['pos']} | 😐 {s['sentiments']['neu']} | ❌ {s['sentiments']['neg']}")
    lines.append("Примечание: «доля в посте» — % кликов пункта от всех кликов «Смотреть» в его посте.")
    return "\n".join(lines)

# ---------- Commands ----------
async def cmd_postnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("Недостаточно прав.")
    await post_digest(update, context)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("Недостаточно прав.")
    days = 7
    if context.args:
        try:
            days = max(1, min(30, int(context.args[0])))
        except:
            pass
    s = stats_last_days(days)
    text = fmt_stats(days, s)
    target_chat = _target_report_chat(update)
    if target_chat and str(target_chat) != str(update.effective_chat.id):
        await context.bot.send_message(chat_id=target_chat, text=text)
        return await update.message.reply_text("Сводка отправлена в REPORT_CHAT_ID.")
    else:
        return await update.message.reply_text(text)

async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("Недостаточно прав.")
    if not context.args:
        return await update.message.reply_text("Формат: /links <post_id>")
    try:
        post_id = int(context.args[0])
    except:
        return await update.message.reply_text("post_id должен быть числом (message_id поста в канале).")

    c = conn.cursor()
    c.execute("""SELECT item_idx, target_url, r.id
                 FROM redirects r
                 WHERE r.post_msg_id=? ORDER BY item_idx ASC""", (post_id,))
    rows = c.fetchall()
    if not rows:
        return await update.message.reply_text("Редиректов для этого поста не найдено.")

    lines = [f"🔗 Ссылки по посту {post_id}:"]
    for item_idx, url, rid in rows:
        c.execute("SELECT COUNT(*) FROM redirect_hits WHERE redirect_id=?", (rid,))
        cnt = c.fetchone()[0] or 0
        lines.append(f"#{item_idx}: {url}\nКликов: {cnt}")
    await update.message.reply_text("\n\n".join(lines), disable_web_page_preview=True)

async def cmd_export_clicks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("Недостаточно прав.")

    # Форматы:
    # /export_clicks                       → последние 7 дней, все посты
    # /export_clicks 2025-11-01 2025-11-10 → период, все посты
    # /export_clicks 2025-11-01 2025-11-10 12345 → период, только post_id=12345
    args = context.args or []
    post_id_filter = None

    if len(args) >= 2:
        d1 = parse_date(args[0]); d2 = parse_date(args[1])
        if not d1 or not d2:
            return await update.message.reply_text("Формат дат: YYYY-MM-DD. Пример: /export_clicks 2025-11-01 2025-11-10 [post_id]")
        if len(args) >= 3:
            try:
                post_id_filter = int(args[2])
            except:
                return await update.message.reply_text("post_id должен быть числом (message_id поста в канале).")
    else:
        d2 = datetime.utcnow()
        d1 = d2 - timedelta(days=7)

    start_ts = int(datetime(d1.year, d1.month, d1.day, 0, 0, 0).timestamp())
    end_ts   = int(datetime(d2.year, d2.month, d2.day, 23, 59, 59).timestamp())

    c = conn.cursor()
    base_sql = """
      SELECT h.ts, r.post_msg_id, r.item_idx, h.ip, h.ua, h.referer, r.token, r.target_url
      FROM redirect_hits h
      JOIN redirects r ON r.id = h.redirect_id
      WHERE h.ts BETWEEN ? AND ?
    """
    params = [start_ts, end_ts]
    if post_id_filter is not None:
        base_sql += " AND r.post_msg_id = ?"
        params.append(post_id_filter)
    base_sql += " ORDER BY h.ts ASC"

    c.execute(base_sql, params)
    rows = c.fetchall()
    if not rows:
        return await update.message.reply_text("За указанный период кликов нет (или неверный post_id).")

    # Пишем CSV
    fd, path = tempfile.mkstemp(prefix="clicks_", suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts_iso","post_msg_id","item_idx","ip","user_agent","referer","token","target_url"])
        for ts, post_id, idx, ip, ua, ref, token, url in rows:
            w.writerow([datetime.utcfromtimestamp(ts).isoformat(), post_id, idx, ip, ua, ref, token, url])

    # Лимит размера → zip при необходимости
    send_path, disp_name = _zip_if_too_big(path, MAX_EXPORT_MB)

    # Куда отправляем отчёт
    target_chat = _target_report_chat(update)
    if target_chat is None:
        return await update.message.reply_text("Не удалось определить чат для отправки отчёта.")

    caption = f"Экспорт кликов {d1.date()} — {d2.date()}"
    if post_id_filter is not None:
        caption += f" | post_id={post_id_filter}"
    if os.path.getsize(send_path) > MAX_EXPORT_MB * 1024 * 1024 and send_path.endswith(".zip"):
        caption += f"\n⚠️ Файл всё ещё больше {MAX_EXPORT_MB} МБ. Сузь диапазон дат или укажи post_id."

    await context.bot.send_document(
        chat_id=target_chat,
        document=InputFile(send_path, filename=disp_name),
        caption=caption
    )
    if str(target_chat) != str(update.effective_chat.id):
        await update.message.reply_text("Готово: отчёт отправлен в REPORT_CHAT_ID.")

def main():
    # Flask редирект
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    # Telegram polling
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("postnow", cmd_postnow))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("export_clicks", cmd_export_clicks))
    app.add_handler(CallbackQueryHandler(on_rate))
    app.run_polling()

if __name__ == "__main__":
    main()
