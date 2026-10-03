# The model lives in its own process: killing that process frees ALL GPU memory (CUDA context included).
# Parent side: Engine. Child side: `python worker.py <model>` (talks pickle over stdin/stdout).
import os, pickle, subprocess, sys, threading

class Engine:
    def __init__(self, model): self.model, self.p, self.lock, self.io = model, None, threading.Lock(), threading.Lock()

    def alive(self): return self.p is not None and self.p.poll() is None

    def start(self):  # idempotent; blocks until the model is loaded (concurrent callers wait for the one load)
        with self.lock:
            if self.alive(): return
            exe = os.path.join(os.path.dirname(sys.executable), "python.exe")  # python.exe + no-window flag: reliable pipes
            self.p = subprocess.Popen([exe, os.path.abspath(__file__), self.model], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=open("app.log", "a"), creationflags=subprocess.CREATE_NO_WINDOW)
            try: msg = pickle.load(self.p.stdout)
            except EOFError: msg = ("err", "worker exited, see app.log")
            if msg[0] != "ready": self.stop(); raise RuntimeError(msg[1])

    def transcribe(self, audio, **opts):  # raw text; starts the worker if needed
        self.start()
        with self.io:
            try:
                pickle.dump((audio, opts), self.p.stdin); self.p.stdin.flush()
                kind, val = pickle.load(self.p.stdout)
            except (EOFError, OSError): raise RuntimeError("worker stopped during transcription")
        if kind == "err": raise RuntimeError(val)
        return val

    def stop(self):  # taskkill /T: the venv python.exe is a launcher, the real interpreter is its child
        p, self.p = self.p, None
        if p and p.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            p.wait()

if __name__ == "__main__":
    inp, out = os.fdopen(os.dup(0), "rb"), os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # stray prints from native libraries must not corrupt the pipe
    try:
        from faster_whisper import WhisperModel
        try: m = WhisperModel(sys.argv[1], device="cuda", compute_type="int8_float16")
        except Exception: m = WhisperModel(sys.argv[1], device="cpu", compute_type="int8")  # e.g. VRAM full: slow but works
        pickle.dump(("ready",), out); out.flush()
    except Exception as e:
        pickle.dump(("err", repr(e)), out); out.flush(); sys.exit(1)
    while True:
        try: audio, opts = pickle.load(inp)
        except EOFError: break  # parent closed the pipe (or died): exit so the GPU is released
        try: msg = ("ok", " ".join(s.text.strip() for s in m.transcribe(audio, **opts)[0]))
        except Exception as e: msg = ("err", repr(e))
        pickle.dump(msg, out); out.flush()
