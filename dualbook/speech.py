"""
Local text-to-speech for the voice simulator.

WHY THIS EXISTS
---------------
`run_voice.py simulate` runs the real voice agent — same prompt, same slots,
same booking write — but printed its replies, which reasonably left people
asking "shouldn't I be hearing this?". You should. This makes the simulator
speak out loud using whatever the operating system already provides, so you can
hear the agent without paying a voice provider.

WHAT IT IS NOT
--------------
This is local playback, not telephony. A real call also needs speech-to-text
for the caller and a phone network to carry it — that's what Uplift AI or Vapi
provide. Here you still type; the agent talks back.

Backends, in order of preference. All are free and offline:
    pyttsx3          cross-platform, if the user happens to have it
    Windows SAPI     via PowerShell System.Speech — built in, nothing to install
    macOS `say`      built in
    Linux            espeak / spd-say, if present
"""

from __future__ import annotations

import atexit
import base64
import logging
import platform
import shutil
import subprocess
import threading
import time

log = logging.getLogger(__name__)

# A LONG-LIVED PowerShell worker, not one process per line.
#
# `Add-Type -AssemblyName System.Speech` loads and JIT-compiles the speech
# assembly, which measured ~30 SECONDS per invocation on a cold Windows box.
# Paying that per reply makes the agent unusable. Started once, it costs a few
# seconds on the first line and is near-instant after.
#
# Each utterance arrives base64-encoded on stdin, so apostrophes, quotes,
# newlines and non-ASCII can't break the protocol or be interpreted by the
# shell. The worker echoes a marker when it finishes speaking, which is what
# lets speak() block until the audio has actually played.
_PS_WORKER = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = {rate}
[Console]::Out.WriteLine('<<READY>>')
[Console]::Out.Flush()
while ($true) {{
  $line = [Console]::In.ReadLine()
  if ($null -eq $line -or $line -eq '<<QUIT>>') {{ break }}
  if ($line.Length -gt 0) {{
    try {{
      $s.Speak([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line)))
    }} catch {{ }}
  }}
  [Console]::Out.WriteLine('<<DONE>>')
  [Console]::Out.Flush()
}}
$s.Dispose()
"""


class Speaker:
    """Speaks text aloud, or degrades to silence with one clear warning."""

    def __init__(self, rate: int = 1, enabled: bool = True) -> None:
        self.rate = rate
        self.enabled = enabled
        self._backend = self._pick_backend() if enabled else None
        self._proc = None
        self._warned = False
        self._lock = threading.Lock()
        # Don't leave a PowerShell process behind if the caller forgets, or on
        # Ctrl-C out of a conversation.
        atexit.register(self.close)
        if enabled and self._backend == "sapi":
            self.warm_up()

    def warm_up(self) -> None:
        """
        Start the speech worker in the background, now.

        Loading the speech assembly takes ~20 seconds on a cold machine. Doing
        it lazily means the caller hears nothing for 20 seconds after the agent
        has already answered. Starting it here overlaps that cost with the
        first LLM call and the user reading the banner, so by the time there is
        something to say, the synthesiser is usually ready.
        """
        def _boot() -> None:
            try:
                self._sapi_worker()
            except Exception:
                log.debug("speech warm-up failed", exc_info=True)

        threading.Thread(target=_boot, daemon=True, name="tts-warmup").start()

    # -- backend selection --------------------------------------------------

    @staticmethod
    def _pick_backend() -> str | None:
        try:
            import pyttsx3  # noqa: F401
            return "pyttsx3"
        except Exception:
            pass

        system = platform.system()
        if system == "Windows":
            return "sapi"
        if system == "Darwin" and shutil.which("say"):
            return "say"
        for cmd in ("espeak-ng", "espeak", "spd-say"):
            if shutil.which(cmd):
                return cmd
        return None

    @property
    def available(self) -> bool:
        return self._backend is not None

    def describe(self) -> str:
        return {
            "pyttsx3": "pyttsx3",
            "sapi": "Windows System.Speech",
            "say": "macOS say",
        }.get(self._backend or "", self._backend or "unavailable")

    # -- speaking -----------------------------------------------------------

    def speak(self, text: str) -> None:
        """
        Speak one line. Blocking on purpose: replies should be heard in order,
        and overlapping utterances are worse than a short wait.

        Never raises — losing audio must not take the conversation down with it.
        """
        if not self.enabled or not text.strip():
            return
        if not self._backend:
            self._warn_once("No text-to-speech backend found — running silent.")
            return

        try:
            if self._backend == "pyttsx3":
                self._speak_pyttsx3(text)
            elif self._backend == "sapi":
                self._speak_sapi(text)
            elif self._backend == "say":
                subprocess.run(["say", text], check=False, timeout=90)
            else:
                subprocess.run([self._backend, text], check=False, timeout=90)
        except Exception as exc:  # audio is a nicety, the booking is not
            self._warn_once(f"Text-to-speech failed ({exc}) — continuing silently.")

    def _speak_pyttsx3(self, text: str) -> None:
        import pyttsx3

        # Re-created per utterance: a long-lived pyttsx3 engine is prone to
        # deadlocking on repeated runAndWait() calls on Windows.
        engine = pyttsx3.init()
        engine.setProperty("rate", 165 + (self.rate * 15))
        engine.say(text)
        engine.runAndWait()
        engine.stop()

    def _sapi_worker(self):
        """
        Return the persistent PowerShell synthesiser, starting it if needed.

        Locked because warm_up() races with the first speak(): without it both
        would spawn a worker and one would be orphaned.
        """
        with self._lock:
            return self._sapi_worker_locked()

    def _sapi_worker_locked(self):
        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        creation = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation = subprocess.CREATE_NO_WINDOW  # no console flash on Windows

        self._proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _PS_WORKER.format(rate=self.rate)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, creationflags=creation,
        )
        # Wait for the assembly to finish loading so the first reply isn't
        # spoken into a process that isn't listening yet.
        deadline = time.time() + 40
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                break
            if line.strip() == "<<READY>>":
                return self._proc
        self._proc = None
        raise RuntimeError("speech worker did not start")

    def _speak_sapi(self, text: str) -> None:
        proc = self._sapi_worker()
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        proc.stdin.write(encoded + "\n")
        proc.stdin.flush()
        # Block until it reports the utterance finished, so replies don't
        # overlap and the transcript stays in step with the audio.
        deadline = time.time() + 120
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line or line.strip() == "<<DONE>>":
                return

    def close(self) -> None:
        """Shut the worker down. Safe to call more than once."""
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.write("<<QUIT>>\n")
                proc.stdin.flush()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    def __enter__(self) -> "Speaker":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _warn_once(self, message: str) -> None:
        if not self._warned:
            self._warned = True
            log.warning(message)


# ---------------------------------------------------------------------------
# Speech-to-text — the caller's half of the conversation
# ---------------------------------------------------------------------------

# Windows ships a speech recogniser (System.Speech.Recognition) that runs
# entirely offline, so the caller can actually talk without any install, model
# download or paid API. Same persistent-worker pattern as the synthesiser, for
# the same reason: loading the assembly is slow and must happen once.
#
# `Recognize()` returns as soon as it detects a complete phrase followed by
# silence, which is what makes turn-taking feel natural.
_PS_LISTENER = r"""
Add-Type -AssemblyName System.Speech
$r = New-Object System.Speech.Recognition.SpeechRecognitionEngine
try {{
  $r.SetInputToDefaultAudioDevice()
}} catch {{
  [Console]::Out.WriteLine('<<NOMIC>>')
  [Console]::Out.Flush()
  exit
}}
$r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$r.InitialSilenceTimeout = [TimeSpan]::FromSeconds({initial})
$r.EndSilenceTimeout = [TimeSpan]::FromSeconds(1.0)
[Console]::Out.WriteLine('<<READY>>')
[Console]::Out.Flush()
while ($true) {{
  $line = [Console]::In.ReadLine()
  if ($null -eq $line -or $line -eq '<<QUIT>>') {{ break }}
  try {{
    $res = $r.Recognize([TimeSpan]::FromSeconds({timeout}))
    if ($res -and $res.Text) {{
      $b = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($res.Text))
      [Console]::Out.WriteLine('<<TEXT>>' + $b)
    }} else {{
      [Console]::Out.WriteLine('<<NONE>>')
    }}
  }} catch {{
    [Console]::Out.WriteLine('<<NONE>>')
  }}
  [Console]::Out.Flush()
}}
$r.Dispose()
"""


class Listener:
    """
    Captures one spoken utterance at a time from the default microphone.

    Windows-only by design: it's the one recogniser available with nothing to
    install. Elsewhere `available` is False and the caller falls back to typing,
    which is why every call site treats speech as optional rather than assumed.
    """

    def __init__(self, enabled: bool = True, timeout: float = 12.0) -> None:
        self.enabled = enabled and platform.system() == "Windows"
        self.timeout = timeout
        self._proc = None
        self._no_mic = False
        self._lock = threading.Lock()
        atexit.register(self.close)
        if self.enabled:
            self.warm_up()

    @property
    def available(self) -> bool:
        return self.enabled and not self._no_mic

    def describe(self) -> str:
        if not self.enabled:
            return "unavailable (Windows only)"
        return "no microphone" if self._no_mic else "Windows System.Speech.Recognition"

    def warm_up(self) -> None:
        def _boot() -> None:
            try:
                self._worker()
            except Exception:
                log.debug("listener warm-up failed", exc_info=True)

        threading.Thread(target=_boot, daemon=True, name="stt-warmup").start()

    def _worker(self):
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._proc
            if self._no_mic:
                return None

            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 _PS_LISTENER.format(initial=8.0, timeout=self.timeout)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                bufsize=1, creationflags=creation,
            )
            deadline = time.time() + 45
            while time.time() < deadline:
                line = self._proc.stdout.readline()
                if not line:
                    break
                token = line.strip()
                if token == "<<READY>>":
                    return self._proc
                if token == "<<NOMIC>>":
                    log.warning("No microphone available — falling back to typing.")
                    self._no_mic = True
                    self._proc = None
                    return None
            self._proc = None
            raise RuntimeError("speech recogniser did not start")

    def listen(self) -> str | None:
        """
        Block until one utterance is recognised.

        Returns the text, or None when nothing was heard — the caller then asks
        them to repeat or type, rather than sending an empty turn to the model.
        """
        if not self.enabled:
            return None
        try:
            proc = self._worker()
        except Exception as exc:
            log.warning("Speech recognition unavailable (%s) — type instead.", exc)
            self.enabled = False
            return None
        if proc is None:
            return None

        try:
            proc.stdin.write("<<LISTEN>>\n")
            proc.stdin.flush()
            deadline = time.time() + self.timeout + 20
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    return None
                token = line.strip()
                if token.startswith("<<TEXT>>"):
                    return base64.b64decode(token[8:]).decode("utf-8").strip() or None
                if token == "<<NONE>>":
                    return None
        except Exception as exc:
            log.warning("Speech recognition failed (%s) — type instead.", exc)
            self.enabled = False
        return None

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.write("<<QUIT>>\n")
                proc.stdin.flush()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    def __enter__(self) -> "Listener":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
