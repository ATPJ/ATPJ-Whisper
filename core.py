# Core logic (no GUI): config, history/dictionary DB, word corrections, transcription. Self-check: `uv run core.py --test`
import os, sys, re, json, time, wave, difflib, sqlite3, threading
import numpy as np

os.chdir(os.environ.get("WHISPER_HOME") or os.path.dirname(os.path.abspath(__file__)))  # config/db/recordings live next to the code

CFG = {"hotkey": "f9", "mode": "auto", "language": "fa", "model": "large-v3", "fuzzy": 0.8, "restore_clipboard": False,
       "idle_unload_minutes": 5, "typing_wpm": 40, "proxy": ""}  # mode: auto (hold = push-to-talk, double-tap = hands-free) | hold | toggle; proxy (e.g. "http://127.0.0.1:8080") is only used to download the model
if os.path.exists("config.json"): CFG.update(json.load(open("config.json", encoding="utf-8")))
else: json.dump(CFG, open("config.json", "w", encoding="utf-8"), indent=2)
if CFG["proxy"]:
    os.environ.setdefault("HTTPS_PROXY", CFG["proxy"]); os.environ.setdefault("HTTP_PROXY", CFG["proxy"])

# Latin examples in the prompt stop Whisper from transliterating English words into Persian script.
PROMPT = ("سلام، من دارم با Python و JavaScript یک پروژه روی GitHub می‌سازم. فایل README رو آپدیت کردم "
          "و API جدید رو با Docker deploy کردم. لطفاً commit و push رو چک کن و بعد pull request بزن.")
# Persian spellings Whisper tends to produce -> the English word. Extend it by fixing a transcript in the app (Save edit) or here.
SEED = {"پایتون": "Python", "جاوااسکریپت": "JavaScript", "جاوا‌اسکریپت": "JavaScript", "گیت‌هاب": "GitHub",
        "گیت": "Git", "داکر": "Docker", "ریکت": "React", "لینوکس": "Linux", "اندروید": "Android",
        "آیفون": "iPhone", "گوگل": "Google", "یوتیوب": "YouTube", "واتساپ": "WhatsApp", "تلگرام": "Telegram",
        "اینستاگرام": "Instagram", "اکسل": "Excel", "پاورپوینت": "PowerPoint", "کامیت": "commit", "پوش": "push",
        "ریپازیتوری": "repository", "برنچ": "branch", "دیپلوی": "deploy", "سرور": "server", "فرانت‌اند": "frontend",
        "بک‌اند": "backend", "دیتابیس": "database", "باگ": "bug", "فریمورک": "framework", "لایبرری": "library",
        "ای‌پی‌آی": "API", "چت‌جی‌پی‌تی": "ChatGPT", "کلود": "Claude", "اوپن‌ای‌آی": "OpenAI", "ویسپر": "Whisper"}
os.makedirs("recordings", exist_ok=True)
db = sqlite3.connect(":memory:" if "--test" in sys.argv else "history.db", check_same_thread=False)
db.execute("create table if not exists history(id integer primary key, text text, audio text, at default (datetime('now','localtime')))")
db.execute("create table if not exists words(wrong text primary key, right text)")  # wrong==right -> plain vocabulary
_cols = {r[1] for r in db.execute("pragma table_info(history)")}  # analytics columns (added to older databases)
if "secs" not in _cols: db.execute("alter table history add column secs real")
if "edited" not in _cols: db.execute("alter table history add column edited integer default 0")
for _id, _path in db.execute("select id, audio from history where secs is null").fetchall():  # backfill from the WAV files
    try:
        with wave.open(_path) as _w: _secs = _w.getnframes() / _w.getframerate()
    except Exception: _secs = 0
    db.execute("update history set secs=? where id=?", (_secs, _id))
db.commit()

for p, e in SEED.items():  # both with and without ZWNJ; "insert or ignore" keeps the user's own corrections
    db.executemany("insert or ignore into words values(?,?)", [(p, e), (p.replace("‌", ""), e), (e, e)])
db.commit()

class Gesture:  # turns raw hotkey press/release into start/stop. auto: hold = push-to-talk, quick double-tap = hands-free (tap again to stop)
    def __init__(self, start, stop, mode="auto", tap=0.3, gap=0.4):
        self.start, self.stop, self.mode, self.tap, self.gap = start, stop, mode, tap, gap
        self.state, self.t0, self.timer, self.lock = "idle", 0, None, threading.RLock()  # idle | held | pending (short tap, waiting for 2nd) | locked

    def press(self):
        with self.lock:
            if self.state == "held": return  # key auto-repeat
            if self.state == "locked": self.state = "idle"; return self.stop()
            if self.state == "pending": self.timer.cancel(); self.state = "locked"; return  # 2nd tap: keep recording hands-free
            self.t0 = time.time(); self.start()
            self.state = "locked" if self.mode == "toggle" else "held"

    def release(self):
        with self.lock:
            if self.state != "held": return
            if self.mode == "auto" and time.time() - self.t0 < self.tap:  # short tap: maybe the first half of a double-tap
                self.state = "pending"; self.timer = threading.Timer(self.gap, self.expire); self.timer.start()
            else: self.state = "idle"; self.stop()

    def expire(self):  # a lone short tap is an accident, not a dictation
        with self.lock:
            if self.state == "pending": self.state = "idle"; self.stop(discard=True)

    def reset(self):  # recording was stopped some other way (Esc)
        with self.lock:
            if self.timer: self.timer.cancel()
            self.state = "idle"

def tokens(s): return re.findall(r"[\w‌]+", s)

def vocab(): return [r for (r,) in db.execute("select distinct right from words")]

def fix(text):  # exact corrections first, then nearest vocabulary word
    fixes = dict(db.execute("select wrong, right from words where wrong != right"))
    low = {v.lower(): v for v in vocab()}
    def sub(m):
        w = m.group(0)
        if w in fixes: return fixes[w]
        c = difflib.get_close_matches(w.lower(), list(low), n=1, cutoff=CFG["fuzzy"]) if len(w) >= 4 else []
        return low[c[0]] if c else w
    return re.sub(r"[\w‌]+", sub, text)

def learn(old, new):  # 1:1 word swaps in a manual edit become corrections; returns the pairs learned
    a, b, got = tokens(old), tokens(new), []
    lat = lambda w: bool(re.search("[A-Za-z]", w))
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "replace" and i2 - i1 == j2 - j1:
            for x, y in zip(a[i1:i2], b[j1:j2]):  # skip rewordings: only typo-like or Persian->English swaps are corrections
                if lat(x) != lat(y) or difflib.SequenceMatcher(None, x, y).ratio() >= 0.6:
                    db.executemany("insert or replace into words values(?,?)", [(x, y), (y, y)]); got.append((x, y))
    db.commit(); return got

def transcribe(engine, audio):  # engine = worker.Engine; corrections are applied here (the worker has no DB)
    return fix(engine.transcribe(audio, language=CFG["language"], vad_filter=True, beam_size=5,
                                 condition_on_previous_text=False, initial_prompt=PROMPT if CFG["language"] == "fa" else None,
                                 hotwords=" ".join(vocab()[-50:]) or None))

def add_history(text, audio):  # saves the recording as WAV + a history row; returns the row id
    path = f"recordings/{time.strftime('%Y%m%d-%H%M%S')}.wav"
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("int16").tobytes())
    rid = db.execute("insert into history(text, audio, secs) values(?,?,?)", (text, path, len(audio) / 16000)).lastrowid; db.commit()
    return rid

def stats():  # numbers for the Analytics tab
    import datetime as dt
    rows = db.execute("select date(at), text, coalesce(secs, 0), edited from history").fetchall()
    today, iso = dt.date.today(), lambda d: d.isoformat()
    def agg(sel):
        r = [x for x in rows if sel(x[0])]
        words = sum(len(tokens(x[1])) for x in r); secs = sum(x[2] for x in r)
        timed = sum(len(tokens(x[1])) for x in r if x[2] > 0)  # old rows without audio can't be timed
        return dict(words=words, n=len(r), secs=secs, wpm=timed / (secs / 60) if secs else 0,
                    saved=max(0, timed / CFG["typing_wpm"] * 60 - secs))  # typing time you avoided minus time spent talking
    week0 = iso(today - dt.timedelta(days=6))
    per = {"today": agg(lambda d: d == iso(today)), "week": agg(lambda d: d >= week0), "all": agg(lambda d: True)}
    byday = {}
    for d, t, *_ in rows: byday[d] = byday.get(d, 0) + len(tokens(t))
    days = [iso(today - dt.timedelta(days=i)) for i in range(13, -1, -1)]
    day = today if iso(today) in byday else today - dt.timedelta(days=1)  # a streak survives until the day ends
    streak = 0
    while iso(day) in byday: streak += 1; day -= dt.timedelta(days=1)
    return dict(periods=per, daily=[(d, byday.get(d, 0)) for d in days], streak=streak,
                clean=sum(1 for x in rows if not x[3]) / len(rows) if rows else None)  # share of dictations never edited

if __name__ == "__main__" and "--test" in sys.argv:
    learn("من با پایتون کار میکنم", "من با Python کار میکنم")
    assert fix("پایتون خوبه") == "Python خوبه"
    db.execute("insert into words values('Kubernetes','Kubernetes')")
    assert fix("Kubernetas") == "Kubernetes" and fix("the cat") == "the cat"
    assert learn("کتاب خوب", "میز خوب") == [] and fix("کتاب") == "کتاب"  # rewording is not learned
    assert fix("من پایتون و گیت‌هاب") == "من Python و GitHub"
    w80 = " ".join(["کلمه"] * 80)  # 80 words in 60 s = 80 wpm; typing at 40 wpm would take 120 s -> 60 s saved
    db.execute("insert into history(text, audio, secs) values(?, '', 60)", (w80,))
    db.execute("insert into history(text, audio, secs, edited, at) values(?, '', 60, 1, datetime('now','localtime','-1 day'))", (w80,))
    st = stats(); t = st["periods"]["today"]
    assert t["words"] == 80 and round(t["wpm"]) == 80 and round(t["saved"]) == 60 and t["n"] == 1
    assert st["periods"]["all"]["words"] == 160 and st["streak"] == 2 and st["clean"] == 0.5 and st["daily"][-1][1] == 80
    ev = []; gs = Gesture(lambda: ev.append("start"), lambda discard=False: ev.append("discard" if discard else "stop"), tap=0.1, gap=0.15)
    gs.press(); gs.press(); time.sleep(0.12); gs.release(); assert ev == ["start", "stop"]  # hold (auto-repeat ignored)
    ev.clear(); gs.press(); gs.release(); gs.press(); gs.release(); time.sleep(0.2); assert ev == ["start"]  # double-tap: locked, still recording
    gs.press(); assert ev == ["start", "stop"] and gs.state == "idle"  # next press stops
    ev.clear(); gs.press(); gs.release(); time.sleep(0.25); assert ev == ["start", "discard"]  # lone tap discarded
    ev.clear(); gs.mode = "toggle"; gs.press(); gs.release(); gs.press(); assert ev == ["start", "stop"]
    print("ok"); sys.exit()

