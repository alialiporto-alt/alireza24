import os
import re
import sqlite3
import secrets
import string
import logging
from datetime import datetime, timezone
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
DB_PATH = os.getenv("DB_PATH", "exam_bot.db")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("exam-bot")

# Conversation states
ADMIN_PASSWORD_STATE, BULK_STUDENTS, EXAM_TITLE, EXAM_CODE, EXAM_START, EXAM_END, EXAM_DURATION, QUESTION_TEXT, QUESTION_TYPE, QUESTION_OPTIONS, QUESTION_ANSWER, QUESTION_POINTS, GRADE_STUDENT, GRADE_VALUE = range(14)

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS students(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            access_code TEXT UNIQUE NOT NULL,
            telegram_id INTEGER UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS exams(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            exam_code TEXT UNIQUE NOT NULL,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            qtype TEXT NOT NULL CHECK(qtype IN ('mcq','essay')),
            body TEXT NOT NULL,
            options TEXT,
            answer TEXT,
            points REAL NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS exam_codes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            code TEXT UNIQUE NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            UNIQUE(exam_id, student_id)
        );
        CREATE TABLE IF NOT EXISTS attempts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL REFERENCES exams(id),
            student_id INTEGER NOT NULL REFERENCES students(id),
            started_at TEXT NOT NULL,
            submitted_at TEXT,
            status TEXT NOT NULL DEFAULT 'in_progress',
            score REAL,
            UNIQUE(exam_id, student_id)
        );
        CREATE TABLE IF NOT EXISTS answers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL REFERENCES questions(id),
            answer TEXT,
            awarded_points REAL,
            UNIQUE(attempt_id, question_id)
        );
        CREATE TABLE IF NOT EXISTS admin_sessions(
            telegram_id INTEGER PRIMARY KEY,
            logged_in INTEGER NOT NULL DEFAULT 0
        );
        """)

def now_iso():
    return datetime.now().astimezone().isoformat(timespec="minutes")

def parse_local_dt(s):
    # Accept YYYY-MM-DD HH:MM in the server's local timezone.
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").astimezone()

def make_code(length=8):
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def unique_code(table, column, length=8):
    while True:
        code = make_code(length)
        with db() as c:
            exists = c.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (code,)).fetchone()
        if not exists:
            return code

def is_admin(update):
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)

def admin_logged_in(uid):
    with db() as c:
        r = c.execute("SELECT logged_in FROM admin_sessions WHERE telegram_id=?", (uid,)).fetchone()
    return bool(r and r["logged_in"])

def admin_only(fn):
    @wraps(fn)
    async def wrapper(update, context, *args, **kwargs):
        if not is_admin(update) or not admin_logged_in(update.effective_user.id):
            if update.callback_query:
                await update.callback_query.answer("دسترسی مجاز نیست.", show_alert=True)
            else:
                await update.effective_message.reply_text("⛔ دسترسی مدیریت ندارید.")
            return ConversationHandler.END
        return await fn(update, context, *args, **kwargs)
    return wrapper

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎓 ورود دانشجو", callback_data="student_login")],
        [InlineKeyboardButton("📝 شرکت در آزمون", callback_data="take_exam")],
        [InlineKeyboardButton("📊 مشاهده نمرات", callback_data="my_scores")],
        [InlineKeyboardButton("🔐 ورود مدیریت", callback_data="admin_login")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 ثبت گروهی دانشجویان", callback_data="admin_bulk")],
        [InlineKeyboardButton("🧪 ساخت آزمون", callback_data="admin_new_exam")],
        [InlineKeyboardButton("🔑 کدهای آزمون", callback_data="admin_codes")],
        [InlineKeyboardButton("📊 نتایج و پاسخ‌های دانشجویان", callback_data="admin_results")],
        [InlineKeyboardButton("🛠 تغییر یا حذف آزمون", callback_data="exam_manage")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎓 به سامانه آزمون آنلاین خوش آمدید.\n\nاز منوی زیر گزینه موردنظر را انتخاب کنید:",
        reply_markup=main_keyboard()
    )

async def home_callback(update, context):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🏠 منوی اصلی سامانه", reply_markup=main_keyboard())

async def student_login_start(update, context):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("کد شناسایی اختصاصی دانشجویی خود را وارد کنید:")
    return 100

async def student_login_code(update, context):
    code = update.message.text.strip().upper()
    uid = update.effective_user.id
    with db() as c:
        student = c.execute("SELECT * FROM students WHERE access_code=?", (code,)).fetchone()
        if not student:
            await update.message.reply_text("❌ کد دانشجویی معتبر نیست.")
            return ConversationHandler.END
        if student["telegram_id"] is None:
            c.execute("UPDATE students SET telegram_id=? WHERE id=?", (uid, student["id"]))
            name = student["name"]
        elif student["telegram_id"] == uid:
            name = student["name"]
        else:
            await update.message.reply_text("⛔ این کد قبلاً به حساب تلگرام دیگری متصل شده است.")
            return ConversationHandler.END
    context.user_data["student_id"] = student["id"]
    await update.message.reply_text(f"✅ خوش آمدی {name}!\nحساب دانشجویی شما فعال است.", reply_markup=main_keyboard())
    return ConversationHandler.END

async def take_exam_start(update, context):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    with db() as c:
        student = c.execute("SELECT * FROM students WHERE telegram_id=?", (uid,)).fetchone()
    if not student:
        await q.message.reply_text("ابتدا از بخش «ورود دانشجو» حساب خود را فعال کنید.")
        return ConversationHandler.END
    context.user_data["student_id"] = student["id"]
    await q.message.reply_text("کد اختصاصی آزمون را وارد کنید:")
    return 101

async def take_exam_code(update, context):
    code = update.message.text.strip().upper()
    student_id = context.user_data.get("student_id")
    with db() as c:
        row = c.execute("""
            SELECT e.title, e.starts_at, e.ends_at, e.duration_minutes,
                   e.active, e.id AS eid
            FROM exams e WHERE UPPER(e.exam_code)=? AND e.active=1
        """, (code,)).fetchone()
        if not row:
            await update.message.reply_text("❌ کد درس/آزمون معتبر نیست یا آزمون غیرفعال شده است.")
            return ConversationHandler.END
        existing = c.execute("SELECT id,status FROM attempts WHERE exam_id=? AND student_id=?",
                             (row["eid"], student_id)).fetchone()
        if existing:
            await update.message.reply_text("⚠️ شما قبلاً در این آزمون شرکت کرده‌اید. برای شرکت مجدد با مدیر تماس بگیرید.")
            return ConversationHandler.END
        now = datetime.now().astimezone()
        start_dt = datetime.fromisoformat(row["starts_at"])
        end_dt = datetime.fromisoformat(row["ends_at"])
        if not row["active"] or now < start_dt or now > end_dt:
            await update.message.reply_text("⏰ آزمون در حال حاضر فعال نیست یا مهلت آن تمام شده است.")
            return ConversationHandler.END
        questions = c.execute("SELECT * FROM questions WHERE exam_id=? ORDER BY position", (row["eid"],)).fetchall()
        if not questions:
            await update.message.reply_text("این آزمون هنوز سؤالی ندارد.")
            return ConversationHandler.END
        cur = c.execute("INSERT INTO attempts(exam_id,student_id,started_at) VALUES(?,?,?)",
                        (row["eid"], student_id, now.isoformat(timespec="minutes")))
        attempt_id = cur.lastrowid
    context.user_data["attempt_id"] = attempt_id
    context.user_data["exam_id"] = row["eid"]
    context.user_data["questions"] = [dict(x) for x in questions]
    context.user_data["q_index"] = 0
    context.user_data["deadline"] = min(now.timestamp() + row["duration_minutes"]*60, end_dt.timestamp())
    await update.message.reply_text(f"✅ آزمون «{row['title']}» شروع شد.\nمدت مجاز: {row['duration_minutes']} دقیقه")
    await send_current_question(update, context)
    return 102

async def send_current_question(update, context):
    qs = context.user_data["questions"]
    i = context.user_data["q_index"]
    if i >= len(qs):
        await submit_attempt(update, context, automatic=False)
        return
    q = qs[i]
    text = f"سؤال {i+1} از {len(qs)}\n\n{q['body']}\n\nنمره: {q['points']}"
    if q["qtype"] == "mcq":
        opts = (q["options"] or "").split("|")
        kb = [[InlineKeyboardButton(f"{chr(65+j)}) {opt}", callback_data=f"ans_{q['id']}_{j}")]
              for j, opt in enumerate(opts)]
        kb.append([InlineKeyboardButton("⏭ پایان آزمون", callback_data="finish_exam")])
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        context.user_data["awaiting_essay"] = q["id"]
        await update.effective_message.reply_text(text + "\n\nپاسخ تشریحی خود را در یک پیام ارسال کنید.")

async def answer_callback(update, context):
    q = update.callback_query
    await q.answer()
    if datetime.now().timestamp() > context.user_data.get("deadline", 0):
        await submit_attempt(update, context, automatic=True)
        return ConversationHandler.END
    if q.data == "finish_exam":
        await submit_attempt(update, context, automatic=False)
        return ConversationHandler.END
    _, qid, opt_index = q.data.split("_")
    qid, opt_index = int(qid), int(opt_index)
    questions = context.user_data["questions"]
    question = next((x for x in questions if x["id"] == qid), None)
    if not question:
        return 102
    options = (question["options"] or "").split("|")
    chosen = options[opt_index] if opt_index < len(options) else ""
    correct = (question["answer"] or "").strip().upper()
    letter = chr(65 + opt_index)
    points = float(question["points"]) if correct == letter else 0.0
    with db() as c:
        c.execute("INSERT OR REPLACE INTO answers(attempt_id,question_id,answer,awarded_points) VALUES(?,?,?,?)",
                  (context.user_data["attempt_id"], qid, letter, points))
    context.user_data["q_index"] += 1
    await q.message.reply_text("پاسخ ثبت شد.")
    await send_current_question(update, context)
    return 102

async def essay_answer(update, context):
    qid = context.user_data.get("awaiting_essay")
    if not qid:
        return ConversationHandler.END
    if datetime.now().timestamp() > context.user_data.get("deadline", 0):
        await submit_attempt(update, context, automatic=True)
        return ConversationHandler.END
    with db() as c:
        c.execute("INSERT OR REPLACE INTO answers(attempt_id,question_id,answer,awarded_points) VALUES(?,?,?,NULL)",
                  (context.user_data["attempt_id"], qid, update.message.text))
    context.user_data.pop("awaiting_essay", None)
    context.user_data["q_index"] += 1
    await update.message.reply_text("پاسخ تشریحی ثبت شد.")
    await send_current_question(update, context)
    return 102

async def submit_attempt(update, context, automatic=False):
    attempt_id = context.user_data.get("attempt_id")
    if not attempt_id:
        return
    with db() as c:
        c.execute("UPDATE attempts SET submitted_at=?, status=? WHERE id=?",
                  (now_iso(), "submitted" if not automatic else "auto_submitted", attempt_id))
        total = c.execute("""
            SELECT COALESCE(SUM(a.awarded_points),0) AS score,
                   SUM(CASE WHEN a.awarded_points IS NULL THEN 1 ELSE 0 END) AS pending
            FROM answers a WHERE a.attempt_id=?
        """, (attempt_id,)).fetchone()
        c.execute("UPDATE attempts SET score=? WHERE id=?", (total["score"], attempt_id))
    await update.effective_message.reply_text(
        f"🏁 آزمون ثبت شد.\nنمره قطعی فعلی: {total['score']}\n"
        "پاسخ‌های تشریحی پس از بررسی مدیر به نمره نهایی اضافه می‌شوند."
    )
    context.user_data.pop("attempt_id", None)
    context.user_data.pop("questions", None)

async def my_scores(update, context):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    with db() as c:
        student = c.execute("SELECT id,name FROM students WHERE telegram_id=?", (uid,)).fetchone()
        if not student:
            await q.message.reply_text("ابتدا وارد حساب دانشجویی شوید.")
            return
        rows = c.execute("""
            SELECT e.title, a.score, a.status, a.submitted_at,
                   (SELECT COUNT(*) FROM answers an JOIN questions qu ON qu.id=an.question_id
                    WHERE an.attempt_id=a.id AND qu.qtype='essay' AND an.awarded_points IS NULL) AS pending
            FROM attempts a JOIN exams e ON e.id=a.exam_id
            WHERE a.student_id=? ORDER BY a.id DESC
        """, (student["id"],)).fetchall()
    if not rows:
        await q.message.reply_text("هنوز نمره‌ای برای شما ثبت نشده است.")
        return
    lines = [f"📘 {r['title']}: {r['score'] if r['score'] is not None else 0} نمره" +
             (" (در انتظار تصحیح تشریحی)" if r["pending"] else "") for r in rows]
    await q.message.reply_text("📊 کارنامه شما\n\n" + "\n".join(lines))

async def admin_login_start(update, context):
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        await q.message.reply_text("⛔ شناسه تلگرام شما مدیر ثبت‌شده نیست.")
        return ConversationHandler.END
    await q.message.reply_text("رمز مدیریت را وارد کنید:")
    return ADMIN_PASSWORD_STATE

async def admin_password(update, context):
    if update.message.text != ADMIN_PASSWORD or not ADMIN_PASSWORD:
        await update.message.reply_text("❌ رمز اشتباه است.")
        return ConversationHandler.END
    with db() as c:
        c.execute("INSERT OR REPLACE INTO admin_sessions(telegram_id,logged_in) VALUES(?,1)",
                  (update.effective_user.id,))
    await update.message.reply_text("✅ ورود مدیر موفق بود.", reply_markup=admin_keyboard())
    return ConversationHandler.END

@admin_only
async def admin_bulk_start(update, context):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("نام هر دانشجو را در یک خط بفرستید. مثال:\nعلی رضایی\nسارا کریمی")
    return BULK_STUDENTS

@admin_only
async def admin_bulk_save(update, context):
    names = [x.strip() for x in update.message.text.splitlines() if x.strip()]
    created = []
    with db() as c:
        for name in names:
            code = unique_code("students", "access_code", 8)
            c.execute("INSERT INTO students(name,access_code,created_at) VALUES(?,?,?)",
                      (name, code, now_iso()))
            created.append((name, code))
    msg = "✅ دانشجویان ثبت شدند:\n\n" + "\n".join(f"{n} — `{code}`" for n, code in created)
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_keyboard())
    return ConversationHandler.END

@admin_only
async def new_exam_start(update, context):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("عنوان آزمون را وارد کنید:")
    return EXAM_TITLE

@admin_only
async def new_exam_title(update, context):
    context.user_data["new_exam"] = {"title": update.message.text.strip()}
    await update.message.reply_text("کد عمومی درس/آزمون را وارد کنید (مثلاً MATH101):")
    return EXAM_CODE

@admin_only
async def new_exam_code(update, context):
    context.user_data["new_exam"]["exam_code"] = update.message.text.strip().upper()
    await update.message.reply_text("زمان شروع را به شکل YYYY-MM-DD HH:MM وارد کنید:")
    return EXAM_START

@admin_only
async def new_exam_start_dt(update, context):
    try:
        context.user_data["new_exam"]["starts_at"] = parse_local_dt(update.message.text).isoformat(timespec="minutes")
    except ValueError:
        await update.message.reply_text("فرمت اشتباه است. نمونه: 2026-11-20 09:00")
        return EXAM_START
    await update.message.reply_text("زمان پایان را به شکل YYYY-MM-DD HH:MM وارد کنید:")
    return EXAM_END

@admin_only
async def new_exam_end_dt(update, context):
    try:
        end = parse_local_dt(update.message.text)
        start = datetime.fromisoformat(context.user_data["new_exam"]["starts_at"])
        if end <= start:
            raise ValueError()
        context.user_data["new_exam"]["ends_at"] = end.isoformat(timespec="minutes")
    except ValueError:
        await update.message.reply_text("زمان پایان باید بعد از شروع باشد. فرمت: YYYY-MM-DD HH:MM")
        return EXAM_END
    await update.message.reply_text("مدت آزمون به دقیقه را وارد کنید (مثلاً 60):")
    return EXAM_DURATION

@admin_only
async def new_exam_duration(update, context):
    try:
        minutes = int(update.message.text.strip())
        if minutes < 1 or minutes > 600:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("مدت باید عددی بین 1 تا 600 دقیقه باشد.")
        return EXAM_DURATION
    d = context.user_data.pop("new_exam")
    d["duration_minutes"] = minutes
    with db() as c:
        cur = c.execute("""INSERT INTO exams(title,exam_code,starts_at,ends_at,duration_minutes,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (d["title"], d["exam_code"], d["starts_at"], d["ends_at"], minutes, now_iso()))
        exam_id = cur.lastrowid
    await update.message.reply_text(
        f"✅ آزمون ساخته شد.\\nعنوان: {d['title']}\\nکد مشترک درس/آزمون: {d['exam_code']}\\n"
        "همه دانشجویان پس از ورود با کد دانشجویی، همین کد را وارد می‌کنند.\\n"
        "برای افزودن سؤال از /addquestion استفاده کنید.",
        reply_markup=admin_keyboard()
    )
    return ConversationHandler.END

@admin_only
async def add_question_command(update, context):
    with db() as c:
        exams = c.execute("SELECT id,title FROM exams ORDER BY id DESC").fetchall()
    if not exams:
        await update.message.reply_text("ابتدا آزمون بسازید.")
        return
    rows = [[InlineKeyboardButton(x["title"], callback_data=f"choose_exam_{x['id']}")] for x in exams]
    await update.message.reply_text("آزمون موردنظر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))

@admin_only
async def choose_exam(update, context):
    q = update.callback_query
    await q.answer()
    exam_id = int(q.data.rsplit("_", 1)[1])
    context.user_data["editing_exam_id"] = exam_id
    await q.message.reply_text("متن سؤال را وارد کنید:")
    return QUESTION_TEXT

@admin_only
async def question_text(update, context):
    context.user_data["question_body"] = update.message.text.strip()
    await update.message.reply_text("نوع سؤال را بنویسید: تستی یا تشریحی")
    return QUESTION_TYPE

@admin_only
async def question_type(update, context):
    t = update.message.text.strip()
    if t not in ("تستی", "تشریحی"):
        await update.message.reply_text("فقط «تستی» یا «تشریحی» را وارد کنید.")
        return QUESTION_TYPE
    context.user_data["question_type"] = "mcq" if t == "تستی" else "essay"
    if t == "تستی":
        await update.message.reply_text("چهار گزینه را در چهار خط وارد کنید:")
        return QUESTION_OPTIONS
    await update.message.reply_text("نمره سؤال را وارد کنید:")
    return QUESTION_POINTS

@admin_only
async def question_options(update, context):
    opts = [x.strip() for x in update.message.text.splitlines() if x.strip()]
    if len(opts) != 4:
        await update.message.reply_text("باید دقیقاً چهار گزینه، هرکدام در یک خط، وارد کنید.")
        return QUESTION_OPTIONS
    context.user_data["question_options"] = "|".join(opts)
    await update.message.reply_text("پاسخ صحیح را با A یا B یا C یا D وارد کنید:")
    return QUESTION_ANSWER

@admin_only
async def question_answer(update, context):
    a = update.message.text.strip().upper()
    if a not in ("A", "B", "C", "D"):
        await update.message.reply_text("فقط یکی از A، B، C یا D را وارد کنید.")
        return QUESTION_ANSWER
    context.user_data["question_answer"] = a
    await update.message.reply_text("نمره سؤال را وارد کنید:")
    return QUESTION_POINTS

@admin_only
async def question_points(update, context):
    try:
        points = float(update.message.text.strip())
        if points <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("نمره باید عددی بزرگ‌تر از صفر باشد.")
        return QUESTION_POINTS
    d = context.user_data
    with db() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 AS p FROM questions WHERE exam_id=?",
                        (d["editing_exam_id"],)).fetchone()["p"]
        c.execute("""INSERT INTO questions(exam_id,position,qtype,body,options,answer,points)
                     VALUES(?,?,?,?,?,?,?)""",
                  (d["editing_exam_id"], pos, d["question_type"], d["question_body"],
                   d.get("question_options"), d.get("question_answer"), points))
    for k in ("question_body","question_type","question_options","question_answer"):
        d.pop(k, None)
    await update.message.reply_text("✅ سؤال ثبت شد. برای سؤال بعدی /addquestion را بزنید.",
                                    reply_markup=admin_keyboard())
    return ConversationHandler.END

@admin_only
async def admin_codes(update, context):
    q = update.callback_query
    await q.answer()
    with db() as c:
        rows = c.execute("SELECT title,exam_code,starts_at,ends_at,active FROM exams ORDER BY id DESC").fetchall()
    if not rows:
        await q.message.reply_text("هنوز آزمونی ساخته نشده است.")
        return
    lines = [f"{r['title']} | کد مشترک: {r['exam_code']} | {r['starts_at']} تا {r['ends_at']} | {'فعال' if r['active'] else 'غیرفعال'}" for r in rows]
    await q.message.reply_text("🔑 کدهای مشترک درس/آزمون:\n\n" + "\n".join(lines[:50]))

@admin_only
async def admin_results(update, context):
    q = update.callback_query
    await q.answer()
    with db() as c:
        rows = c.execute("""
            SELECT a.id,e.title,s.name,a.score,a.status,
            (SELECT COUNT(*) FROM answers an JOIN questions qu ON qu.id=an.question_id
             WHERE an.attempt_id=a.id AND qu.qtype='essay' AND an.awarded_points IS NULL) AS pending
            FROM attempts a JOIN exams e ON e.id=a.exam_id JOIN students s ON s.id=a.student_id
            ORDER BY a.id DESC LIMIT 50
        """).fetchall()
    if not rows:
        await q.message.reply_text("هنوز آزمونی ثبت نشده است.")
        return
    lines = [f"#{r['id']} {r['name']} | {r['title']} | نمره: {r['score'] or 0} | تشریحیِ بررسی‌نشده: {r['pending']}" for r in rows]
    with db() as c:
        details = c.execute("""
            SELECT s.name,e.title,q.position,q.body,an.answer
            FROM answers an JOIN attempts a ON a.id=an.attempt_id
            JOIN students s ON s.id=a.student_id JOIN exams e ON e.id=a.exam_id
            JOIN questions q ON q.id=an.question_id
            ORDER BY a.id DESC,q.position
        """).fetchall()
    answer_lines = [f"{r['name']} | {r['title']} | سؤال {r['position']}: {r['body']}\nپاسخ: {r['answer']}" for r in details[:30]]
    await q.message.reply_text("📊 نتایج اخیر:\n\n" + "\n".join(lines) +
                               "\n\n📝 پاسخ‌های ثبت‌شده:\n" +
                               ("\n\n".join(answer_lines) if answer_lines else "هنوز پاسخی ثبت نشده است.") +
                               "\n\nبرای تصحیح تشریحی از /grade استفاده کنید.")

@admin_only
async def grade_command(update, context):
    with db() as c:
        rows = c.execute("""
            SELECT an.id AS answer_id,a.id AS attempt_id,s.name,e.title,q.body,an.answer,q.points
            FROM answers an JOIN attempts a ON a.id=an.attempt_id
            JOIN students s ON s.id=a.student_id JOIN exams e ON e.id=a.exam_id
            JOIN questions q ON q.id=an.question_id
            WHERE q.qtype='essay' AND an.awarded_points IS NULL ORDER BY an.id
        """).fetchall()
    if not rows:
        await update.message.reply_text("پاسخ تشریحیِ بررسی‌نشده‌ای وجود ندارد.")
        return
    context.user_data["grading_queue"] = [dict(r) for r in rows]
    context.user_data["grading_index"] = 0
    await show_grade_item(update, context)
    return GRADE_VALUE

async def show_grade_item(update, context):
    rows = context.user_data["grading_queue"]
    i = context.user_data["grading_index"]
    if i >= len(rows):
        await update.effective_message.reply_text("✅ همه پاسخ‌های موجود بررسی شدند.")
        return
    r = rows[i]
    await update.effective_message.reply_text(
        f"تصحیح {i+1} از {len(rows)}\nدانشجو: {r['name']}\nآزمون: {r['title']}\n"
        f"سؤال: {r['body']}\nپاسخ: {r['answer']}\nحداکثر نمره: {r['points']}\n\n"
        "نمره اعطایی را وارد کنید:"
    )

@admin_only
async def grade_value(update, context):
    try:
        value = float(update.message.text.strip())
        r = context.user_data["grading_queue"][context.user_data["grading_index"]]
        if value < 0 or value > float(r["points"]):
            raise ValueError()
    except (ValueError, IndexError):
        await update.message.reply_text("نمره نامعتبر است؛ عددی بین صفر و حداکثر نمره سؤال وارد کنید.")
        return GRADE_VALUE
    r = context.user_data["grading_queue"][context.user_data["grading_index"]]
    with db() as c:
        c.execute("UPDATE answers SET awarded_points=? WHERE id=?", (value, r["answer_id"]))
        score = c.execute("SELECT COALESCE(SUM(awarded_points),0) AS s FROM answers WHERE attempt_id=?",
                          (r["attempt_id"],)).fetchone()["s"]
        c.execute("UPDATE attempts SET score=? WHERE id=?", (score, r["attempt_id"]))
    context.user_data["grading_index"] += 1
    await show_grade_item(update, context)
    return GRADE_VALUE

@admin_only
async def exam_manage_start(update, context):
    q = update.callback_query
    await q.answer()
    with db() as c:
        exams = c.execute("SELECT id,title,exam_code,starts_at,ends_at FROM exams ORDER BY id DESC").fetchall()
    if not exams:
        await q.message.reply_text("هنوز آزمونی ثبت نشده است.")
        return
    kb = []
    for e in exams:
        kb.append([InlineKeyboardButton(f"✏️ تغییر تاریخ: {e['title']}", callback_data=f"exam_edit_{e['id']}")])
        kb.append([InlineKeyboardButton(f"🗑 حذف آزمون: {e['title']}", callback_data=f"exam_delete_{e['id']}")])
    await q.message.reply_text("آزمون موردنظر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))

@admin_only
async def exam_edit_start(update, context):
    q = update.callback_query
    await q.answer()
    exam_id = int(q.data.rsplit("_", 1)[1])
    with db() as c:
        e = c.execute("SELECT id,title,starts_at,ends_at FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not e:
        await q.message.reply_text("آزمون پیدا نشد.")
        return ConversationHandler.END
    context.user_data["exam_edit_id"] = exam_id
    await q.message.reply_text(f"تغییر تاریخ آزمون «{e['title']}»\nزمان شروع فعلی: {e['starts_at']}\nزمان پایان فعلی: {e['ends_at']}\n\nزمان شروع جدید را با قالب YYYY-MM-DD HH:MM وارد کنید:")
    return 201

@admin_only
async def exam_edit_start_date(update, context):
    try:
        context.user_data["exam_new_start"] = parse_local_dt(update.message.text).isoformat(timespec="minutes")
    except ValueError:
        await update.message.reply_text("فرمت تاریخ اشتباه است؛ نمونه: 2026-11-20 09:00")
        return 201
    await update.message.reply_text("زمان پایان جدید را با قالب YYYY-MM-DD HH:MM وارد کنید:")
    return 202

@admin_only
async def exam_edit_end_date(update, context):
    try:
        end = parse_local_dt(update.message.text)
        start = datetime.fromisoformat(context.user_data["exam_new_start"])
        if end <= start:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("زمان پایان باید بعد از شروع باشد. نمونه: 2026-11-20 12:00")
        return 202
    with db() as c:
        c.execute("UPDATE exams SET starts_at=?,ends_at=? WHERE id=?",
                  (context.user_data.pop("exam_new_start"), end.isoformat(timespec="minutes"),
                   context.user_data.pop("exam_edit_id")))
    await update.message.reply_text("✅ تاریخ آزمون تغییر کرد.", reply_markup=admin_keyboard())
    return ConversationHandler.END

@admin_only
async def exam_delete_confirm(update, context):
    q = update.callback_query
    await q.answer()
    exam_id = int(q.data.rsplit("_", 1)[1])
    with db() as c:
        e = c.execute("SELECT title FROM exams WHERE id=?", (exam_id,)).fetchone()
        if not e:
            await q.message.reply_text("آزمون پیدا نشد.")
            return
        c.execute("DELETE FROM answers WHERE attempt_id IN (SELECT id FROM attempts WHERE exam_id=?)", (exam_id,))
        c.execute("DELETE FROM attempts WHERE exam_id=?", (exam_id,))
        c.execute("DELETE FROM exams WHERE id=?", (exam_id,))
    await q.message.reply_text(f"🗑 آزمون «{e['title']}» و سؤال‌ها، پاسخ‌ها و سوابق شرکت در آن حذف شدند.", reply_markup=admin_keyboard())

async def logout(update, context):
    uid = update.effective_user.id
    with db() as c:
        c.execute("DELETE FROM admin_sessions WHERE telegram_id=?", (uid,))
    await update.effective_message.reply_text("از پنل مدیریت خارج شدید.")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است.")
    if not ADMIN_ID or not ADMIN_PASSWORD:
        raise RuntimeError("ADMIN_ID و ADMIN_PASSWORD باید تنظیم شوند.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("addquestion", add_question_command))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(student_login_start, pattern="^student_login$")],
        states={100: [MessageHandler(filters.TEXT & ~filters.COMMAND, student_login_code)]},
        fallbacks=[CommandHandler("start", start)],
        per_user=True
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(take_exam_start, pattern="^take_exam$")],
        states={
            101: [MessageHandler(filters.TEXT & ~filters.COMMAND, take_exam_code)],
            102: [
                CallbackQueryHandler(answer_callback, pattern="^(ans_\\d+_\\d+|finish_exam)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, essay_answer)
            ]
        },
        fallbacks=[CommandHandler("start", start)],
        per_user=True
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_login_start, pattern="^admin_login$")],
        states={ADMIN_PASSWORD_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_password)]},
        fallbacks=[CommandHandler("start", start)],
        per_user=True
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_bulk_start, pattern="^admin_bulk$")],
        states={BULK_STUDENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bulk_save)]},
        fallbacks=[CommandHandler("start", start)],
        per_user=True
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(new_exam_start, pattern="^admin_new_exam$")],
        states={
            EXAM_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_exam_title)],
            EXAM_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_exam_code)],
            EXAM_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_exam_start_dt)],
            EXAM_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_exam_end_dt)],
            EXAM_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_exam_duration)]
        },
        fallbacks=[CommandHandler("start", start)],
        per_user=True
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addquestion", add_question_command),
                      CallbackQueryHandler(choose_exam, pattern="^choose_exam_\\d+$")],
        states={
            QUESTION_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, question_text)],
            QUESTION_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, question_type)],
            QUESTION_OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, question_options)],
            QUESTION_ANSWER: [MessageHandler(filters.TEXT & ~filters.COMMAND, question_answer)],
            QUESTION_POINTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, question_points)]
        },
        fallbacks=[CommandHandler("start", start)],
        per_user=True
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("grade", grade_command)],
        states={GRADE_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, grade_value)]},
        fallbacks=[CommandHandler("start", start)],
        per_user=True
    ))
    app.add_handler(CallbackQueryHandler(home_callback, pattern="^home$"))
    app.add_handler(CallbackQueryHandler(my_scores, pattern="^my_scores$"))
    app.add_handler(CallbackQueryHandler(admin_codes, pattern="^admin_codes$"))
    app.add_handler(CallbackQueryHandler(admin_results, pattern="^admin_results$"))
    app.add_handler(CallbackQueryHandler(exam_manage_start, pattern="^exam_manage$"))
    app.add_handler(CallbackQueryHandler(exam_delete_confirm, pattern="^exam_delete_\\d+$"))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(exam_edit_start, pattern="^exam_edit_\\d+$")],
        states={
            201: [MessageHandler(filters.TEXT & ~filters.COMMAND, exam_edit_start_date)],
            202: [MessageHandler(filters.TEXT & ~filters.COMMAND, exam_edit_end_date)]
        },
        fallbacks=[CommandHandler("start", start)],
        per_user=True
    ))
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
