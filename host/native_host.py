"""yt-dl-ext の Native Messaging ホスト。

Chrome が拡張の要求に応じて起動し、stdin/stdout でやり取りする。
拡張側が接続を切ればこのプロセスも終わるので、常駐しない。

プロトコル: [4バイト little-endian の長さ][UTF-8 の JSON] の繰り返し。
"""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_OUT_DIR = Path(os.path.expanduser("~")) / "Downloads" / "yt-dl-ext"
FALLBACK_OUT_DIR = Path(tempfile.gettempdir()) / "yt-dl-ext"
CONFIG_FILE = Path(__file__).with_name("config.json")

YTDLP = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
FFMPEG = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")

PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<pct>[\d.]+)%(?:\s+of\s+~?\S+)?"
    r"(?:\s+at\s+(?P<speed>\S+))?(?:\s+ETA\s+(?P<eta>\S+))?"
)
DEST_RE = re.compile(r"\[(?:download|ExtractAudio)\]\s+Destination:\s*(.+)$")
# 結合後の最終ファイル: [Merger] Merging formats into "C:\...\foo.mp4"
MERGE_RE = re.compile(r"\[Merger\]\s+Merging formats into\s+\"(.+)\"")

ALLOWED_PREFIXES = (
    "https://www.youtube.com/", "https://youtube.com/", "https://m.youtube.com/",
    "https://youtu.be/", "https://music.youtube.com/",
)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# yt-dlp は Python 製なので、標準出力がパイプに繋がれていると Windows では
# ロケールの文字コード（日本語環境では cp932）で書き出す。こちらは UTF-8 として
# 読むので、日本語のタイトルやファイル名がそのまま化ける。UTF-8 モードを
# 環境変数で強制して、出力側とデコード側を揃える。
CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
SEND_LOCK = threading.Lock()

# YTDLEXT_DEBUG=1 で host\debug.log に記録する。Chrome から起動されると
# 標準エラーがどこにも出ないので、不具合を追うにはこれしか手がない。
DEBUG = os.environ.get("YTDLEXT_DEBUG") == "1"
DEBUG_LOG = Path(__file__).with_name("debug.log")
LOG_LOCK = threading.Lock()  # SEND_LOCK と共用すると send 内から呼べなくなる


def log(*parts, force=False):
    """force=True の行は YTDLEXT_DEBUG 無しでも残す。
    Chrome がホストを起動できたのか自体を後から判別できないと詰むため。"""
    if not (DEBUG or force):
        return
    with LOG_LOCK:
        if DEBUG_LOG.exists() and DEBUG_LOG.stat().st_size > 2_000_000:
            DEBUG_LOG.unlink()
        with open(DEBUG_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"{datetime.now():%m-%d %H:%M:%S.%f} " + " ".join(map(str, parts)) + "\n")

if sys.platform == "win32":  # stdio を必ずバイナリにする
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

STDIN = sys.stdin.buffer
STDOUT = sys.stdout.buffer


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def out_dir():
    """保存先。設定が無効なら既定に落とす（保存先が消えていても落ちないように）。"""
    for candidate in (load_config().get("outDir"), DEFAULT_OUT_DIR, FALLBACK_OUT_DIR):
        if not candidate:
            continue
        p = Path(candidate)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            continue
    return FALLBACK_OUT_DIR


def set_out_dir(raw):
    """保存先を保存する。(成功したか, メッセージ) を返す。"""
    raw = (raw or "").strip().strip('"')
    if not raw:
        return False, "保存先が空です"
    p = Path(raw)
    if not p.is_absolute():
        return False, f"絶対パスで指定してください（例: {DEFAULT_OUT_DIR}）"
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".ytdlext_write_test"
        probe.touch()
        probe.unlink()
    except Exception as e:
        return False, f"そこには書き込めません: {e}"
    try:
        CONFIG_FILE.write_text(json.dumps({"outDir": str(p)}), encoding="utf-8")
    except Exception as e:
        return False, f"設定を保存できません: {e}"
    log("out dir set to", p)
    return True, str(p)


def send(msg):
    log("send", {k: v for k, v in msg.items() if k != "error"})
    data = json.dumps(msg).encode("utf-8")
    with SEND_LOCK:
        STDOUT.write(struct.pack("<I", len(data)))
        STDOUT.write(data)
        STDOUT.flush()


def read_message():
    head = STDIN.read(4)
    if len(head) < 4:
        return None  # Chrome が接続を切った
    (length,) = struct.unpack("<I", head)
    body = STDIN.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------- yt-dlp の更新
# YouTube の仕様変更で yt-dlp が動かなくなることが定期的にある。
# (1) 週に1回、裏で黙って更新する  (2) ダウンロードが失敗したら更新して1回だけ再試行する
UPDATE_INTERVAL = timedelta(days=7)
RETRY_COOLDOWN = timedelta(minutes=10)  # 失敗のたびに pip を叩かないための間隔
STAMP_FILE = Path(__file__).with_name("last_update.json")


def read_stamp():
    try:
        return json.loads(STAMP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_stamp(**kw):
    data = read_stamp()
    data.update(kw)
    try:
        STAMP_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log("stamp write failed", repr(e))


def ytdlp_version():
    if not YTDLP:
        return None
    try:
        out = subprocess.run([YTDLP, "--version"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=30,
                             creationflags=NO_WINDOW)
        return out.stdout.strip() or None
    except Exception:
        return None


def pip_args():
    return [sys.executable, "-m", "pip", "install", "-U",
            "--disable-pip-version-check", "yt-dlp"]


def update_ytdlp(reason):
    """同期で更新する。バージョンが変わったら True。呼び出し側が待つ前提。"""
    before = ytdlp_version()
    log("update start:", reason, "current=", before)
    write_stamp(checked=datetime.now().isoformat(), attempted=datetime.now().isoformat())
    try:
        out = subprocess.run(pip_args(), stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=300, creationflags=NO_WINDOW)
        log("pip rc=", out.returncode, (out.stdout or "")[-300:])
    except subprocess.TimeoutExpired:
        log("update timeout")
        return False
    after = ytdlp_version()
    write_stamp(version=after)
    log("update done:", before, "->", after)
    return bool(before and after and before != after)


def weekly_update_check():
    """定期更新。ホストは用が済むと数秒で終了するので、pip を道連れにしないよう
    切り離して起動する（途中で殺されると壊れたインストールが残るため）。"""
    last = read_stamp().get("checked")
    if last:
        try:
            if datetime.now() - datetime.fromisoformat(last) < UPDATE_INTERVAL:
                return
        except ValueError:
            pass
    log("weekly update: launching detached pip")
    write_stamp(checked=datetime.now().isoformat())
    flags = NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(pip_args(), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
    except Exception as e:
        log("weekly update failed to start", repr(e))


def update_worth_trying():
    """直近に試して変化が無かったなら、また pip を回しても無駄。"""
    last = read_stamp().get("attempted")
    if not last:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(last) > RETRY_COOLDOWN
    except ValueError:
        return True


def build_args(url, fmt, quality, outtmpl):
    args = [YTDLP, "--no-playlist", "--newline", "--no-part", "-o", outtmpl]
    if FFMPEG:
        args += ["--ffmpeg-location", FFMPEG]

    if fmt == "mp4":
        h = {"best": "", "1080": "[height<=1080]", "720": "[height<=720]",
             "480": "[height<=480]"}.get(quality, "")
        args += [
            "-f", f"bv*{h}[ext=mp4]+ba[ext=m4a]/bv*{h}+ba/b{h}[ext=mp4]/b{h}/b",
            "--merge-output-format", "mp4",
        ]
    elif fmt == "mp3":
        args += ["-f", "ba/b", "-x", "--audio-format", "mp3",
                 "--audio-quality", quality if quality in ("0", "5", "9") else "0"]
    elif fmt == "wav":
        args += ["-f", "ba/b", "-x", "--audio-format", "wav"]
    else:
        raise ValueError(f"unknown format: {fmt}")

    args.append(url)
    return args


def do_download(job_id, url, fmt, quality, retried=False):
    outtmpl = str(out_dir() / "%(title)s.%(ext)s")
    log("do_download enter", job_id, fmt, quality, "retried=", retried)

    try:
        log("args", build_args(url, fmt, quality, outtmpl))
        proc = subprocess.Popen(
            build_args(url, fmt, quality, outtmpl),
            stdin=subprocess.DEVNULL,  # Chrome との stdin パイプを子に継承させない
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=NO_WINDOW, env=CHILD_ENV,
        )
    except Exception as e:
        send({"type": "error", "jobId": job_id, "error": str(e)})
        return

    log("popen ok pid=", proc.pid)
    tail, last_dest = [], None
    for line in proc.stdout:
        line = line.rstrip("\n")
        log("ytdlp>", line)
        tail.append(line)
        del tail[:-15]

        m = PROGRESS_RE.search(line)
        if m:
            send({"type": "progress", "jobId": job_id,
                  "percent": float(m.group("pct")),
                  "speed": m.group("speed"), "eta": m.group("eta")})
            continue
        m = MERGE_RE.search(line) or DEST_RE.search(line)
        if m:
            last_dest = m.group(1).strip()

    if proc.wait() == 0:
        send({"type": "done", "jobId": job_id, "file": last_dest})
        return

    # 失敗の多くは YouTube 側の仕様変更に yt-dlp が追いついていないだけなので、
    # 更新して1回だけやり直す。実際にバージョンが上がったときだけ再試行する。
    if not retried and update_worth_trying():
        send({"type": "status", "jobId": job_id, "text": "yt-dlp を更新しています…"})
        if update_ytdlp("download failed"):
            send({"type": "status", "jobId": job_id, "text": "更新しました。再試行します…"})
            send({"type": "progress", "jobId": job_id, "percent": 0.0,
                  "speed": None, "eta": None})
            do_download(job_id, url, fmt, quality, retried=True)
            return

    send({"type": "error", "jobId": job_id,
          "error": "\n".join(tail) or "yt-dlp が異常終了しました"})


def do_info(req_id, url):
    try:
        # 題名の取得に失敗しても致命的ではないので短めに打ち切る。
        # YouTube 側の応答が数十秒かかることが実際にあった。
        out = subprocess.run(
            [YTDLP, "--no-playlist", "--skip-download", "--socket-timeout", "10",
             "--print", "%(title)s\n%(duration_string)s\n%(uploader)s", url],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=NO_WINDOW, timeout=20, env=CHILD_ENV,
        )
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout).strip()[-400:])
        title, duration, uploader = (out.stdout.strip().split("\n") + ["", "", ""])[:3]
        send({"type": "info", "reqId": req_id, "title": title,
              "duration": duration, "uploader": uploader})
    except subprocess.TimeoutExpired:
        send({"type": "info", "reqId": req_id, "error": "情報の取得がタイムアウトしました"})
    except Exception as e:
        send({"type": "info", "reqId": req_id, "error": str(e)})


def do_reveal(file):
    target = out_dir()
    if file:
        p = Path(file)
        if p.exists() and p.resolve().is_relative_to(target.resolve()):
            subprocess.Popen(["explorer", f"/select,{p.resolve()}"])
            return
    subprocess.Popen(["explorer", str(target)])


def handle(msg):
    kind = msg.get("type")

    if kind == "health":
        send({"type": "health", "ytdlp": bool(YTDLP), "ffmpeg": bool(FFMPEG),
              "outDir": str(out_dir())})

    elif kind == "setOutDir":
        ok, detail = set_out_dir(msg.get("dir"))
        send({"type": "outDir", "reqId": msg.get("reqId"), "ok": ok,
              "outDir": str(out_dir()), "error": None if ok else detail})

    elif kind == "info":
        url = (msg.get("url") or "").strip()
        if not url.startswith(ALLOWED_PREFIXES):
            send({"type": "info", "reqId": msg.get("reqId"),
                  "error": "YouTube の URL ではありません"})
        else:
            threading.Thread(target=do_info, args=(msg.get("reqId"), url),
                             daemon=True).start()

    elif kind == "download":
        url = (msg.get("url") or "").strip()
        job_id = msg.get("jobId") or uuid.uuid4().hex[:12]
        if not url.startswith(ALLOWED_PREFIXES):
            send({"type": "error", "jobId": job_id,
                  "error": "YouTube の URL ではありません"})
        elif not YTDLP:
            send({"type": "error", "jobId": job_id,
                  "error": "yt-dlp が見つかりません: pip install -U yt-dlp"})
        else:
            send({"type": "started", "jobId": job_id})
            threading.Thread(
                target=do_download,
                args=(job_id, url, msg.get("format", "mp4"),
                      str(msg.get("quality", "best"))),
                daemon=True).start()

    elif kind == "reveal":
        do_reveal(msg.get("file"))


def main():
    log("=== host start === python", sys.version.split()[0],
        "ytdlp=", bool(YTDLP), "ffmpeg=", bool(FFMPEG), force=True)
    if YTDLP:
        threading.Thread(target=weekly_update_check, daemon=True).start()
    while True:
        try:
            msg = read_message()
        except Exception as e:
            log("read_message failed", repr(e))
            break
        if msg is None:
            log("=== host exit (stdin closed) ===", force=True)
            break
        log("recv", msg)
        try:
            handle(msg)
        except Exception as e:
            send({"type": "error", "jobId": msg.get("jobId"), "error": repr(e)})


if __name__ == "__main__":
    main()
