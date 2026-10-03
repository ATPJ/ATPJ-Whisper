# PySide6 front end for the offline dictation app. Run: `uv run app.py`  (logic lives in core.py)
import ctypes, os, sys, threading, time, winreg, winsound
import numpy as np, sounddevice as sd, keyboard, pyperclip
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut, QTextOption
from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QHBoxLayout, QTabWidget, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                             QMenu, QMessageBox, QPushButton, QSplitter, QStyle, QSystemTrayIcon, QTextEdit, QVBoxLayout, QWidget)
import core
from worker import Engine
from core import CFG, db, learn

if sys.stdout is None or sys.stderr is None:  # started via pythonw (autostart): no console, so log to a file
    sys.stdout = sys.stderr = open("app.log", "a", encoding="utf-8", buffering=1)

RUN_KEY, RUN_NAME = r"Software\Microsoft\Windows\CurrentVersion\Run", "ATPJWhisper"

def autostart_on():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k: return bool(winreg.QueryValueEx(k, RUN_NAME))
    except OSError: return False

def set_autostart(on):  # per-user Run key; pythonw = no console window
    py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on: winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{py}" "{os.path.abspath(__file__)}" --hidden')
        else:
            try: winreg.DeleteValue(k, RUN_NAME)
            except OSError: pass

COL = {"ok": "#2e7d32", "rec": "#c62828", "busy": "#ef6c00", "info": "#1565c0"}
ACCENT = "QPushButton{background:#1565c0;color:white;font-weight:bold;padding:7px 26px;border:none;border-radius:4px}QPushButton:hover{background:#0d47a1}"

def beep(f): threading.Thread(target=winsound.Beep, args=(f, 50), daemon=True).start()

class Pill(QLabel):  # floating state pill (like Whisper Flow); never takes focus, so paste still lands in your app
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating); self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.timer = QTimer(self, singleShot=True, timeout=self.hide)

    def pop(self, text, color, ms=0):
        self.setStyleSheet(f"background:{color};color:white;font:bold 13pt 'Segoe UI';padding:10px 22px;border-radius:18px")
        self.setText(text); self.adjustSize()
        g = QApplication.primaryScreen().availableGeometry()
        self.move(g.center().x() - self.width() // 2, g.bottom() - 120)
        self.show(); self.timer.stop()
        if ms: self.timer.start(ms)

def dur(s):  # seconds -> "45s" / "4m 12s" / "1h 05m"
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s" if s < 3600 else f"{s // 3600}h {s % 3600 // 60:02d}m"

class Card(QFrame):  # big number + caption
    def __init__(self, caption):
        super().__init__(); self.setFrameShape(QFrame.Shape.StyledPanel)
        self.val = QLabel("—"); self.val.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        cap = QLabel(caption); cap.setStyleSheet("color:gray"); cap.setWordWrap(True)
        v = QVBoxLayout(self); v.addWidget(self.val); v.addWidget(cap)

class Bars(QWidget):  # words per day
    def __init__(self): super().__init__(); self.data = []; self.setMinimumHeight(170)

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(self.data); mx = max((v for _, v in self.data), default=0) or 1
        w, h = self.width() / max(n, 1), self.height() - 40
        for i, (d, v) in enumerate(self.data):
            bh = h * v / mx
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#1565c0"))
            p.drawRoundedRect(QRectF(i * w + w * .15, 20 + h - bh, w * .7, bh), 3, 3)
            p.setPen(self.palette().text().color())
            if v: p.drawText(QRectF(i * w, 20 + h - bh - 18, w, 16), Qt.AlignmentFlag.AlignCenter, str(v))
            p.drawText(QRectF(i * w, self.height() - 18, w, 16), Qt.AlignmentFlag.AlignCenter, d[8:])

class StatsPage(QWidget):
    ROWS = [("Words", lambda p: f"{p['words']:,}"), ("Dictations", lambda p: str(p["n"])),
            ("Recording time", lambda p: dur(p["secs"])), ("Speed (words/min)", lambda p: f"{p['wpm']:.0f}" if p["wpm"] else "—"),
            ("Time saved vs typing", lambda p: dur(p["saved"]))]

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self); v.setContentsMargins(16, 12, 16, 12)
        top = QHBoxLayout(); v.addLayout(top)
        self.cards = {k: Card(c) for k, c in [("words", "Words today"), ("wpm", "Avg speed, words/min"), ("saved", "Time saved, all time"),
                                              ("streak", "Day streak"), ("clean", "Dictations needing no edit")]}
        for c in self.cards.values(): top.addWidget(c)
        grid = QGridLayout(); v.addLayout(grid); self.cells = {}
        for j, h in enumerate(("Today", "Last 7 days", "All time"), 1):
            l = QLabel(h); l.setStyleSheet("font-weight:bold"); l.setAlignment(Qt.AlignmentFlag.AlignRight); grid.addWidget(l, 0, j)
        for i, (name, _) in enumerate(self.ROWS, 1):
            grid.addWidget(QLabel(name), i, 0)
            for j in range(1, 4):
                self.cells[i, j] = QLabel(); self.cells[i, j].setAlignment(Qt.AlignmentFlag.AlignRight); grid.addWidget(self.cells[i, j], i, j)
        for j, st in enumerate((2, 1, 1, 1)): grid.setColumnStretch(j, st)
        v.addWidget(QLabel("Words per day (last 14 days)")); self.bars = Bars(); v.addWidget(self.bars, 1)
        self.note = QLabel(); self.note.setStyleSheet("color:gray"); v.addWidget(self.note)

    def update_data(self):
        s = core.stats(); per = s["periods"]
        self.cards["words"].val.setText(f"{per['today']['words']:,}"); self.cards["wpm"].val.setText(f"{per['all']['wpm']:.0f}")
        self.cards["saved"].val.setText(dur(per["all"]["saved"])); self.cards["streak"].val.setText(str(s["streak"]))
        self.cards["clean"].val.setText("—" if s["clean"] is None else f"{s['clean']:.0%}")
        for i, (_, f) in enumerate(self.ROWS, 1):
            for j, k in enumerate(("today", "week", "all"), 1): self.cells[i, j].setText(f(per[k]))
        self.bars.data = s["daily"]; self.bars.update()
        self.note.setText(f"Time saved assumes you type {CFG['typing_wpm']} words/min (typing_wpm in config.json). "
                          "Speed = words per minute of recording time, including pauses.")

class Win(QMainWindow):
    status_sig = Signal(str, str, str, int)  # worker/hook threads -> GUI thread
    done_sig = Signal(int)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ATPJ Whisper"); self.resize(940, 580)
        self.stream = None; self.chunks = []; self.down = False
        self.engine = Engine(CFG["model"]); self.last_use = time.time()  # engine = model in its own process
        self.state = "idle"  # idle | rec | busy
        self.rows, self.cur = [], None  # cur = history id open in the editor (not the list selection)
        self.pill = Pill()

        self.status = QLabel(); self.status.setMinimumHeight(38)
        self.search = QLineEdit(placeholderText="Search history…", clearButtonEnabled=True)
        self.list = QListWidget()
        left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(8, 8, 4, 8)
        lv.addWidget(QLabel("History  (double-click = copy)")); lv.addWidget(self.search); lv.addWidget(self.list)

        self.info = QLabel()
        self.ed = QTextEdit(acceptRichText=False); self.ed.setFont(QFont("Segoe UI", 13))
        opt = QTextOption(); opt.setTextDirection(Qt.LayoutDirection.LayoutDirectionAuto)  # per-paragraph RTL/LTR
        self.ed.document().setDefaultTextOption(opt)
        self.word = QLineEdit(placeholderText="e.g. Kubernetes")
        self.copy_b = self._btn("Copy", self.copy, "Copy the text above"); self.copy_b.setStyleSheet(ACCENT)
        self.save_b = self._btn("Save edit", self.save_edit, "Ctrl+S"); self.rev_b = self._btn("Revert", self.revert)
        bar = QHBoxLayout()
        for w in (self.copy_b, self._btn("▶ Play", self.play), self._btn("■ Stop", lambda: winsound.PlaySound(None, winsound.SND_PURGE)),
                  self.save_b, self.rev_b): bar.addWidget(w)
        bar.addStretch(); bar.addWidget(self._btn("Delete", self.delete))
        dic = QHBoxLayout(); dic.addWidget(QLabel("Add a word Whisper should know:")); dic.addWidget(self.word, 1)
        dic.addWidget(self._btn("Add", self.add_word))
        right = QWidget(); rv = QVBoxLayout(right); rv.setContentsMargins(4, 8, 8, 8)
        rv.addWidget(self.info); rv.addWidget(self.ed, 1); rv.addLayout(bar); rv.addLayout(dic)

        split = QSplitter(); split.addWidget(left); split.addWidget(right); split.setSizes([320, 620]); split.setStretchFactor(1, 1)
        c = QWidget(); cv = QVBoxLayout(c); cv.setContentsMargins(0, 0, 0, 0); cv.setSpacing(0)
        self.stats = StatsPage(); self.tabs = QTabWidget(); self.tabs.addTab(split, "History"); self.tabs.addTab(self.stats, "Analytics")
        self.tabs.currentChanged.connect(lambda i: i == 1 and self.stats.update_data())
        cv.addWidget(self.status); cv.addWidget(self.tabs, 1); self.setCentralWidget(c)

        self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume), self)
        menu = QMenu(); menu.addAction("Show", self.show_front); auto = menu.addAction("Start with Windows"); auto.setCheckable(True); auto.setChecked(autostart_on())
        auto.toggled.connect(set_autostart)
        menu.addAction("Unload model now (free GPU)", self.unload_now); menu.addAction("Quit", QApplication.quit)
        self.tray.setContextMenu(menu); self.tray.setToolTip("ATPJ Whisper")
        self.tray.activated.connect(lambda r: self.show_front() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

        self.status_sig.connect(self.on_status); self.done_sig.connect(self.on_done)
        self.search.textChanged.connect(self.refresh)
        self.list.currentRowChanged.connect(lambda i: i >= 0 and self.open_row(self.rows[i]))
        self.list.itemDoubleClicked.connect(lambda _: self.copy())
        self.ed.document().modificationChanged.connect(lambda _: self.mark())
        self.word.returnPressed.connect(self.add_word)
        QShortcut(QKeySequence("Ctrl+S"), self, self.save_edit)
        QTimer(self, interval=30000, timeout=self.check_idle).start()
        self.show_text("", False); self.refresh()

    def _btn(self, text, fn, tip=""):
        b = QPushButton(text); b.clicked.connect(lambda: fn()); b.setToolTip(tip); return b

    def show_front(self): self.showNormal(); self.raise_(); self.activateWindow()

    def closeEvent(self, e):  # closing the window keeps the hotkey alive in the tray
        e.ignore(); self.hide()
        self.tray.showMessage("ATPJ Whisper", "Still running in the tray. Right-click the icon to quit.")

    # ---- status ----
    def say(self, t, kind="ok", pop="", ms=0): self.status_sig.emit(t, kind, pop, ms)

    def on_status(self, t, kind, pop, ms):
        self.status.setText(t); self.tray.setToolTip(f"ATPJ Whisper — {t}")
        self.status.setStyleSheet(f"background:{COL[kind]};color:white;font:bold 11pt 'Segoe UI';padding:8px 12px")
        self.pill.pop(pop, COL[kind], ms) if pop else self.pill.hide()

    def ready(self):
        self.state = "idle"
        tail = "" if self.engine.alive() else "  (model reloads on first use)"
        self.say(f"Ready — {'hold' if CFG['mode'] == 'hold' else 'press'} {CFG['hotkey'].upper()} to talk · Esc cancels{tail}")

    def flash(self, t):  # short message, then back to Ready unless something started meanwhile
        self.say(t, "info"); QTimer.singleShot(2500, lambda: self.state == "idle" and self.ready())

    # ---- editor: one record at a time, tied to its id ----
    def row(self): return db.execute("select id, text, audio, at from history where id=?", (self.cur,)).fetchone() if self.cur else None
    def dirty(self): return self.cur is not None and self.ed.document().isModified()

    def mark(self):
        d, r = self.dirty(), self.row()
        self.save_b.setEnabled(d); self.rev_b.setEnabled(d)
        self.info.setText("● Unsaved changes — Ctrl+S to save" if d else (r[3] if r else "Select a recording from the history"))
        self.info.setStyleSheet(f"color:{'#c62828' if d else 'gray'}")

    def show_text(self, t, editable=True):
        self.ed.setPlainText(t); self.ed.document().clearUndoRedoStacks()  # Ctrl+Z never reaches the previous recording
        self.ed.document().setModified(False); self.ed.setReadOnly(not editable); self.mark()

    def open_row(self, r):
        if self.dirty(): self.save_edit(quiet=True)  # never lose edits when switching recordings
        self.cur = r[0] if r else None
        self.show_text(r[1] if r else "", bool(r))

    def pick(self, rid):
        i = next((i for i, r in enumerate(self.rows) if r[0] == rid), None)
        if i is not None:
            self.list.blockSignals(True); self.list.setCurrentRow(i); self.list.blockSignals(False); self.open_row(self.rows[i])

    def refresh(self):
        self.rows = db.execute("select id, text, audio, at from history where text like ? order by id desc",
                               (f"%{self.search.text()}%",)).fetchall()
        self.list.blockSignals(True); self.list.clear()
        for r in self.rows:
            it = QListWidgetItem(f"{r[3][5:16]}   {r[1][:45]}"); self.list.addItem(it)
            if r[0] == self.cur: self.list.setCurrentItem(it)  # keep the open recording highlighted
        self.list.blockSignals(False)

    def save_edit(self, quiet=False):
        r = self.row()
        if r is None: return
        new = self.ed.toPlainText().strip()
        if new != r[1]:
            got = learn(r[1], new)
            db.execute("update history set text=?, edited=1 where id=?", (new, self.cur)); db.commit(); self.refresh()
            if not quiet: self.flash("Saved ✓" + (" — learned: " + ", ".join(f"{x}→{y}" for x, y in got[:3]) if got else ""))
        elif not quiet: self.flash("No changes")
        self.ed.document().setModified(False); self.mark()

    def revert(self):
        if r := self.row(): self.show_text(r[1])

    def copy(self):
        if t := self.ed.toPlainText().strip(): pyperclip.copy(t); self.flash("Copied ✓")

    def play(self):
        if (r := self.row()) and os.path.exists(r[2]): winsound.PlaySound(r[2], winsound.SND_FILENAME | winsound.SND_ASYNC)

    def delete(self):
        r = self.row()
        if r and QMessageBox.question(self, "Delete", "Delete this recording and its text?") == QMessageBox.StandardButton.Yes:
            winsound.PlaySound(None, winsound.SND_PURGE)
            db.execute("delete from history where id=?", (self.cur,)); db.commit()
            if os.path.exists(r[2]): os.remove(r[2])
            self.cur = None; self.refresh(); self.show_text("", False)

    def add_word(self):
        if w := self.word.text().strip():
            db.execute("insert or replace into words values(?,?)", (w, w)); db.commit(); self.word.clear(); self.flash(f"Added “{w}”")

    def on_done(self, rid): self.search.clear(); self.refresh(); self.pick(rid); self.stats.update_data()

    # ---- recording / transcription (hook + worker threads) ----
    def preload(self):  # starts the model process (blocks until loaded)
        try:
            if not self.engine.alive() and self.state != "rec": self.say("Loading model… (first run downloads ~3 GB)", "busy", "Loading model…")
            self.engine.start()
            if self.state == "idle": self.ready()
        except Exception as e: self.say(f"Model load failed: {e}", "rec")

    def unload(self):  # kills the model process: 100% of its GPU memory is released; the next recording starts it again
        self.engine.stop()
        if self.state == "idle": self.ready()

    def unload_now(self):
        if self.state == "idle": self.unload(); self.flash("Model closed — GPU memory freed")
        else: self.flash("Busy — try again in a moment")

    def check_idle(self):
        mins = CFG["idle_unload_minutes"]
        if mins and self.engine.alive() and self.state == "idle" and not self.stream and time.time() - self.last_use > mins * 60: self.unload()

    def start(self):
        if self.stream: return
        self.last_use = time.time(); self.chunks = []; beep(1000)
        if not self.engine.alive(): threading.Thread(target=self.preload, daemon=True).start()  # reload while you speak
        try:
            self.stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                                         callback=lambda d, *_: self.chunks.append(d.copy()))
            self.stream.start()
        except Exception as e: self.stream = None; return self.say(f"Microphone error: {e}", "rec", "Mic error", 2500)
        self.state = "rec"; self.say("Recording…", "rec", "● Recording")

    def stop(self, discard=False):
        s, self.stream = self.stream, None
        if not s: return
        s.stop(); s.close(); beep(600)
        if discard or not self.chunks: return self.ready()
        self.state = "busy"
        threading.Thread(target=self.work, args=(np.concatenate(self.chunks)[:, 0],), daemon=True).start()

    def finish(self, t, pop, kind="info", ms=1200): self.state = "idle"; self.last_use = time.time(); self.say(t, kind, pop, ms)

    def work(self, audio):
        self.say("Transcribing…", "busy", "Transcribing…")
        try:
            if len(audio) < 8000: return self.finish("Too short — hold the key while speaking", "Too short")
            if not self.engine.alive(): self.say("Loading model…", "busy", "Loading model…")
            self.engine.start(); self.say("Transcribing…", "busy", "Transcribing…")
            text = core.transcribe(self.engine, audio)
            if not text: return self.finish("No speech detected", "No speech")
            rid = core.add_history(text, audio)
            self.paste(text); self.done_sig.emit(rid)
            self.finish("Pasted ✓ (also on your clipboard)", "✓ Pasted", "ok")
        except Exception as e: self.finish(f"Error: {e}", "Error", "rec", 2500)

    def paste(self, text):  # clipboard + Ctrl+V (typing mangles Persian); text stays on the clipboard unless restore_clipboard
        old = pyperclip.paste() if CFG["restore_clipboard"] else None
        pyperclip.copy(text); time.sleep(0.08)  # let the clipboard settle and modifier keys release
        keyboard.send("ctrl+v")
        if old is not None: threading.Timer(1.5, pyperclip.copy, (old,)).start()  # slow apps read the clipboard late

    def on_press(self, _):
        if self.down: return  # key auto-repeat
        self.down = True
        self.stop() if CFG["mode"] == "toggle" and self.stream else self.start()

    def on_release(self, _):
        self.down = False
        if CFG["mode"] == "hold": self.stop()

def main():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW(None, False, "ATPJWhisperSingleton")  # two copies would double every hotkey/paste
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS; read before Qt starts and overwrites it
    app = QApplication(sys.argv); app.setQuitOnLastWindowClosed(False)
    if already:
        if "--hidden" not in sys.argv: QMessageBox.information(None, "ATPJ Whisper", "Already running — look for its tray icon (bottom right, maybe under ^).")
        return
    win = Win(); app.aboutToQuit.connect(win.engine.stop)
    if "--hidden" not in sys.argv: win.show()  # autostart begins in the tray
    keyboard.on_press_key(CFG["hotkey"], win.on_press, suppress=True)
    keyboard.on_release_key(CFG["hotkey"], win.on_release, suppress=True)
    keyboard.add_hotkey("esc", lambda: win.stop(discard=True))
    threading.Thread(target=win.preload, daemon=True).start()
    sys.exit(app.exec())

if __name__ == "__main__": main()
