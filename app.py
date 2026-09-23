#!/usr/bin/env python3
"""JodSub — local-first Lao subtitle, translation, SFX, and CapCut handoff app."""
import cgi
import base64
import copy
from difflib import SequenceMatcher
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
import zipfile
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import unquote, urlencode
from editing_style import add_manual_removed_words, analyze_capcut_reference, load_profile as load_editing_profile, save_profile as save_editing_profile, update_profile as update_editing_profile

def urlopen_retry(request, timeout=60, attempts=3):
    """Retry transient DNS/connectivity failures without changing API behavior."""
    last = None
    for attempt in range(max(1, attempts)):
        try:
            return urlrequest.urlopen(request, timeout=timeout)
        except urlerror.HTTPError as exc:
            last = exc
            # Authentication and validation failures are deterministic.  Only
            # retry rate limits, timeouts, and server-side failures.
            if exc.code not in (408, 429) and exc.code < 500:
                raise
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
        except (OSError, urlerror.URLError) as exc:
            last = exc
            reason = getattr(exc, "reason", exc)
            if getattr(reason, "errno", None) not in (8, 101, 110, 111, None):
                raise
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise last

ROOT = Path(__file__).resolve().parent
# In a bundled app, resources are read from the application bundle while all
# user-generated data is kept in the per-user Application Support directory.
# This prevents updates from overwriting projects/settings and keeps API keys
# machine-local (they are stored by the browser, never in the bundle).
PACKAGED = bool(getattr(sys, "frozen", False))
DATA_ROOT = Path(os.environ.get("JODSUB_DATA_DIR", Path.home() / "Library" / "Application Support" / "JodSub")) if PACKAGED else ROOT
# Make the bundled binary visible to Whisper/imageio and any other library
# that invokes the executable by the bare name `ffmpeg`.
os.environ["PATH"] = str(ROOT / "bin") + os.pathsep + os.environ.get("PATH", "")
WORK = DATA_ROOT / "work"
PROJECTS = DATA_ROOT / "projects"
EXPORTS = DATA_ROOT / "exports"
SFX = DATA_ROOT / "sfx"
SFX_RESOURCE = ROOT / "sfx"
EDITING_STYLE_ROOT = DATA_ROOT / "editing_style"
EDITING_STYLE_PROFILE = EDITING_STYLE_ROOT / "profile.json"
EDITING_STYLE_REPORTS = EDITING_STYLE_ROOT / "reports"
KARNSUB_APK = Path(os.environ.get("JODSUB_KARNSUB_APK", str(ROOT / "KarnSub.apk")))
CAPCUT_ROOT = Path.home() / "Movies" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
_dev_lao_model = Path("/Users/apple/Documents/Codex/2026-09-18/new-chat/outputs/LaoCaptioner/lao_models/models--SiangLao--xls-r-lao-asr/snapshots/dfc9daab5b2bdf523b01a937ea31e144af5d23c1")
LAO_AUDITOR_DIR = Path(os.environ.get("JODSUB_LAO_MODEL", str((DATA_ROOT / "models" / "lao-asr") if PACKAGED else _dev_lao_model)))
LAO_AUDITOR = None
WHISPERX_LAO_ALIGNER = None
OPENAI_WHISPER_V3_DIR = (DATA_ROOT / "models" if PACKAGED else ROOT / "models") / "openai-whisper-large-v3"
OPENAI_WHISPER_V3_FILE = OPENAI_WHISPER_V3_DIR / "large-v3.pt"
OPENAI_WHISPER_V3_SIZE = 3_087_371_615  # OpenAI checkpoint byte size.
OPENAI_WHISPER_V3 = None
WHISPERX_V3_DIR = (DATA_ROOT / "models" if PACKAGED else ROOT / "models") / "whisperx-large-v3"
WHISPERX_V3 = None
CAPCUT_USER_DATA = Path.home() / "Movies" / "CapCut" / "User Data"
CAPCUT_ADJUSTMENT_CACHE = CAPCUT_USER_DATA / "Cache" / "onlineMaterial"
CAPCUT_FACE_PRESETS = CAPCUT_USER_DATA / "Presets" / "BeautyFace"
for folder in (WORK, PROJECTS, EXPORTS, SFX, EDITING_STYLE_ROOT, EDITING_STYLE_REPORTS):
    # Packaged installs may start on a clean Mac where DATA_ROOT and its
    # parent directories do not exist yet.
    folder.mkdir(parents=True, exist_ok=True)

def ensure_bundled_sfx():
    """Seed writable per-user SFX storage from bundled defaults once."""
    if not PACKAGED or not SFX_RESOURCE.is_dir():
        return
    try:
        for source in SFX_RESOURCE.rglob("*"):
            if not source.is_file() or source.suffix.lower() not in {".wav", ".mp3", ".ogg", ".m4a"}:
                continue
            target = SFX / source.relative_to(SFX_RESOURCE)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
    except OSError:
        # The app can still run without SFX; surface this only when a feature
        # explicitly needs audio rather than failing during startup.
        pass

ensure_bundled_sfx()
PORT = 8877
STATUS = {"state": "idle", "message": "ພ້ອມແລ້ວ", "progress": 0, "files": []}

def clean(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "file"

def ffmpeg():
    bundled = [
        ROOT / "bin" / "ffmpeg",
        ROOT.parent.parent / "2026-09-18" / "new-chat" / "outputs" / "LaoCaptioner" / "bin" / "ffmpeg",
        Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg"),
    ]
    for candidate in bundled:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which("ffmpeg")
    if found: return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise RuntimeError("ບໍ່ພົບ FFmpeg. ຕິດຕັ້ງ FFmpeg ກ່ອນ export video.")

def fonts():
    names = {"Noto Sans Lao", "Noto Sans Lao Looped", "Noto Serif Lao"}
    for root in (Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"):
        if root.is_dir():
            for ext in ("*.ttf", "*.otf", "*.ttc"):
                names.update(p.stem for p in root.rglob(ext))
    return sorted(names, key=str.casefold)

def sec(value):
    h, m, s = value.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)

def stamp(value):
    ms = max(0, round(value * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def parse_srt(path):
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    pattern = re.compile(r"\d+\s*\n(\d\d:\d\d:\d\d[,.]\d\d\d)\s*-->\s*(\d\d:\d\d:\d\d[,.]\d\d\d)\s*\n(.*?)(?=\n\s*\n|\Z)", re.S)
    return [{"start": sec(a), "end": sec(b), "text": re.sub(r"\s+", " ", t).strip()} for a, b, t in pattern.findall(content) if t.strip()]

def write_srt(captions, path):
    blocks = []
    for number, caption in enumerate(captions, 1):
        blocks.append(f"{number}\n{stamp(float(caption['start']))} --> {stamp(float(caption['end']))}\n{caption['text']}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")

def ass_colour(colour, alpha="00"):
    colour = colour.lstrip("#") if isinstance(colour, str) else "FFFFFF"
    if not re.fullmatch(r"[0-9a-fA-F]{6}", colour): colour = "FFFFFF"
    return f"&H{alpha}{colour[4:6]}{colour[2:4]}{colour[:2]}"

def ass_time(value):
    h, remainder = divmod(max(0, value), 3600); m, remainder = divmod(remainder, 60)
    return f"{int(h)}:{int(m):02}:{int(remainder):02}.{int((remainder % 1) * 100):02}"

def write_ass(captions, style, path):
    font = str(style.get("font", "Noto Sans Lao")).replace(",", " ")[:80]
    size = max(18, min(120, int(style.get("size", 52))))
    shadow = max(0, min(12, int(style.get("shadow", 2))))
    header = """[Script Info]\nTitle: JodSub\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"""
    header += f"Style: Karn,{font},{size},{ass_colour(style.get('colour','#ffffff'))},{ass_colour(style.get('colour','#ffffff'))},{ass_colour(style.get('outline','#000000'))},{ass_colour(style.get('background','#000000'),'80')},0,0,0,0,100,100,0,0,1,3,{shadow},2,80,80,80,1\n\n[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    lines = [header]
    effect = style.get("animation", "None")
    for caption in captions:
        text = str(caption["text"]).replace("\n", r"\N").replace("{", r"\{").replace("}", r"\}")
        duration = max(100, int((float(caption["end"]) - float(caption["start"])) * 1000))
        if effect == "Fade": text = r"{\fad(180," + str(min(180, duration // 2)) + r")}" + text
        elif effect == "Pop": text = r"{\fscx65\fscy65\t(0,180,\fscx100\fscy100)}" + text
        elif effect == "Karaoke": text = r"{\k" + str(max(1, duration // max(1, len(text)) // 10)) + "}" + text
        lines.append(f"Dialogue: 0,{ass_time(float(caption['start']))},{ass_time(float(caption['end']))},Karn,,0,0,0,,{text}\n")
    path.write_text("".join(lines), encoding="utf-8")

def project_path(project_id): return PROJECTS / f"{clean(project_id)}.json"
def load_project(project_id): return json.loads(project_path(project_id).read_text(encoding="utf-8"))
def save_project(data): project_path(data["id"]).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

SEMANTIC_SFX = {
    "wrong": ("ຜິດ", "ບໍ່ແມ່ນ", "ຫ້າມ", "ອັນຕະລາຍ", "ผิด", "ไม่", "ห้าม", "danger"),
    "correct": ("ຖືກ", "ຖືກຕ້ອງ", "ສຳເລັດ", "ດີ", "ถูก", "ถูกต้อง", "สำเร็จ"),
    "zoom_in": ("ສຳຄັນ", "ເບິ່ງ", "ຈຸດ", "ນີ້", "สำคัญ", "ดู", "จุด"),
    "zoom_out": ("ສະຫຼຸບ", "ໂດຍລວມ", "ສຸດທ້າຍ", "สรุป", "โดยรวม"),
    "money": ("ເງິນ", "ລາຄາ", "ຈ່າຍ", "ລາຍໄດ້", "ເງິນຕາ", "เงิน", "ราคา", "จ่าย", "รายได้"),
    "ding": ("ໃໝ່", "ເລີ່ມ", "ໄດ້", "ใหม่", "เริ่ม"),
    "click": ("ກົດ", "ເລືອກ", "ລິ້ງ", "ເປີດ", "ປິດ", "กด", "เลือก", "คลิก", "เปิด", "ปิด"),
    "vinggg": ("ສຸດຍອດ", "ຊະນະ", "ວ້າວ", "ສຳເລັດ", "สุดยอด", "ชนะ", "ว้าว"),
}

def semantic_sfx_catalog():
    """Create the eight intentional, named cues locally; arbitrary pack sounds are never used."""
    folder = SFX / "semantic"; folder.mkdir(exist_ok=True)
    # Each expression is an original short tone.  This avoids claiming that an
    # unrelated file in a generic pack means "correct" or "money".
    expressions = {
        "wrong": "aevalsrc=0.26*sin(2*PI*(720-430*t)*t)*exp(-5*t):s=48000:d=0.42",
        "correct": "aevalsrc=0.25*(sin(2*PI*880*t)+sin(2*PI*1320*t))*exp(-5*t):s=48000:d=0.42",
        "zoom_in": "aevalsrc=0.20*sin(2*PI*(180+900*t)*t)*exp(-3*t):s=48000:d=0.50",
        "zoom_out": "aevalsrc=0.20*sin(2*PI*(1080-900*t)*t)*exp(-3*t):s=48000:d=0.50",
        "money": "aevalsrc=0.18*(sin(2*PI*1760*t)+sin(2*PI*2210*t))*exp(-7*t):s=48000:d=0.34",
        "ding": "aevalsrc=0.24*sin(2*PI*1318*t)*exp(-6*t):s=48000:d=0.38",
        "click": "aevalsrc=0.24*sin(2*PI*2400*t)*exp(-32*t):s=48000:d=0.10",
        "vinggg": "aevalsrc=0.22*(sin(2*PI*392*t)+0.5*sin(2*PI*587*t))*exp(-2.8*t):s=48000:d=0.72",
    }
    clips = {}
    for name, expression in expressions.items():
        target = folder / f"{name}.m4a"
        if not target.is_file() or target.stat().st_size < 256:
            subprocess.run([ffmpeg(), "-y", "-f", "lavfi", "-i", expression, "-c:a", "aac", "-b:a", "128k", str(target)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        clips[name] = target
    return clips

def hydrate_karnsub_sfx():
    """Copy the user's attached KarnSub effect library into JodSub once.

    The APK already contains the named sound assets, so no unknown third-party
    download is necessary. Only `assets/sfx/*.wav` entries are read.
    """
    folder = SFX / "karnsub"; folder.mkdir(exist_ok=True)
    if KARNSUB_APK.is_file():
        try:
            with zipfile.ZipFile(KARNSUB_APK) as archive:
                for name in archive.namelist():
                    if name.startswith("assets/flutter_assets/assets/sfx/") and name.lower().endswith(".wav"):
                        target = folder / Path(name).name
                        if not target.exists(): target.write_bytes(archive.read(name))
        except (OSError, zipfile.BadZipFile):
            pass
    return {item.stem.casefold():item for item in folder.glob("*.wav") if item.is_file()}

def sfx_by_meaning():
    """All local SFX remain available; automatic placement selects by meaning."""
    # The active library is the top-level ``sfx`` folder.  Keep the imported
    # KarnSub assets as a fallback, but prefer exact names from the active pack
    # (including mp3 files) so replacing the folder actually changes auto-SFX.
    named = {item.stem.casefold(): item for item in SFX.iterdir()
             if item.is_file() and item.suffix.lower() in {".wav", ".mp3", ".m4a", ".ogg"}}
    named.update({key: value for key, value in hydrate_karnsub_sfx().items() if key not in named})
    synthetic = semantic_sfx_catalog()
    def pick(*names, fallback):
        found = [named[name.casefold()] for name in names if name.casefold() in named]
        return found or [synthetic[fallback]]
    library = {
        "wrong": pick("buzzer", "record_scratch", "glitch", fallback="wrong"),
        "correct": pick("correct", "applause", "magic", fallback="correct"),
        "zoom_in": pick("whoosh", "whoosh2", "whoosh3", "swoosh", fallback="zoom_in"),
        "zoom_out": pick("whoosh8", "whoosh9", "whoosh10", "swoosh2", fallback="zoom_out"),
        "money": pick("cash_register", "cash_register2", fallback="money"),
        "ding": pick("ding", "ding2", "beep", fallback="ding"),
        "click": pick("camera_shutter", "camera_shutter2", "camera_shutter3", fallback="click"),
        "vinggg": pick("vineboom", "wow", "wow2", "badumtss", fallback="vinggg"),
    }
    # The older JodSub library remains selectable for suitable generic impact
    # moments; it is never presented as a falsely named semantic cue.
    extras = sorted((item for item in SFX.glob("*.ogg") if item.is_file()), key=lambda item:item.name)
    if extras: library["impact"] = extras
    return library

def sfx_catalog():
    # Expose the complete KarnSub library in the UI; semantic auto-placement
    # still chooses only meaning-matched cues.
    hydrate_karnsub_sfx()
    return sorted({item for item in SFX.rglob("*") if item.is_file() and item.suffix.lower() in {".wav", ".mp3", ".ogg", ".m4a"}}, key=lambda item:item.name.casefold())

def one_second_sfx(source):
    """Normalize every automatic cue once, so preview, render and CapCut agree."""
    source = Path(source)
    folder = SFX / "one-second"; folder.mkdir(exist_ok=True)
    target = folder / f"{clean(source.stem)}-{source.stat().st_size}.m4a"
    if not target.is_file() or target.stat().st_size < 256:
        subprocess.run([ffmpeg(), "-y", "-i", str(source), "-af", "apad=pad_dur=1,atrim=duration=1", "-t", "1", "-c:a", "aac", "-b:a", "128k", str(target)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return target

def sfx_events(captions, requested, seed):
    """Put only meaningful named cues on prominent subtitle lines, never random effects."""
    count = max(0, min(int(requested or 0), 20))
    if not count: return []
    if not captions: raise RuntimeError("ຕ້ອງມີ subtitle ກ່ອນຈຶ່ງຈັດ SFX ໄດ້.")
    clips, rng = sfx_by_meaning(), random.Random(seed)
    def scored(index):
        text = str(captions[index].get("text", "")).lower()
        matches = [(name, sum(token in text for token in tokens)) for name, tokens in SEMANTIC_SFX.items()]
        kind, score = max(matches, key=lambda item:item[1])
        # Long and punctuated lines are the only fallback emphasis points.
        prominence = score * 100 + min(40, len(text)) + (15 if any(mark in text for mark in "!?") else 0)
        return prominence, kind
    ranked = sorted(range(len(captions)), key=lambda index:(scored(index)[0], -index), reverse=True)
    timeline_end = max(float(caption.get("end", 0)) for caption in captions)
    # Every generated SFX is exactly one second.  Do not select the final
    # subtitle as a cue when it cannot contain a full clip; re-use an earlier
    # prominent line instead so the audio never runs past the video.
    eligible = [index for index in ranked if float(captions[index].get("start", 0)) <= timeline_end - 1.0]
    eligible = eligible or ranked
    # Distribute cues across the complete project duration.  Pick the nearest
    # meaningful subtitle for each slot, but use the slot itself as the audio
    # time so effects never bunch up around a few captions.
    slot_times = [min(max(0.0, timeline_end - 1.0), timeline_end * (number + 1) / (count + 1)) for number in range(count)]
    remaining = set(eligible)
    chosen = []
    for slot in slot_times:
        pool = remaining or set(eligible)
        line = min(pool, key=lambda index: abs(((float(captions[index].get("start", 0)) + float(captions[index].get("end", 0))) / 2.0) - slot))
        chosen.append(line); remaining.discard(line)
    occurrences = {line:chosen.count(line) for line in set(chosen)}; used = {line:0 for line in occurrences}
    neutral = ("ding", "click", "zoom_in", "vinggg", "impact")
    used_kinds = {kind:0 for kind in clips}
    events = []
    for number, line in enumerate(chosen):
        used[line] += 1; caption = captions[line]; start, end = float(caption["start"]), float(caption["end"])
        kind = scored(line)[1] if scored(line)[0] >= 100 else neutral[number % len(neutral)]
        # Keep the opening and closing cues consistent across every future
        # copy: first SFX is Zoom In, final SFX is Zoom Out.
        if number == 0:
            kind = "zoom_in"
        elif number == len(chosen) - 1 and len(chosen) > 1:
            kind = "zoom_out"
        if kind not in clips: kind = "ding"
        if number == 0:
            selected = semantic_sfx_catalog()["zoom_in"]
        elif number == len(chosen) - 1 and len(chosen) > 1:
            selected = semantic_sfx_catalog()["zoom_out"]
        else:
            selected = clips[kind][used_kinds[kind] % len(clips[kind])]; used_kinds[kind] += 1
        clip = one_second_sfx(selected)
        events.append({"time":round(slot_times[number], 6), "file":str(clip), "label":selected.stem, "kind":kind, "caption_index":line})
    return sorted(events, key=lambda event:event["time"])

def lao_tokens(text):
    """Port the LaoCaptioner grouping logic into JodSub without importing that app."""
    text = re.sub(r"\s+", "", str(text))
    if not text: return []
    try:
        from laonlp import word_tokenize
        tokens = [token.strip() for token in word_tokenize(text) if token.strip()]
        return tokens or [text]
    except Exception:
        # Keep text intact rather than inventing unsafe character-level words.
        return [text]

def timed_words(caption):
    """Preserve Gemini's word times; derive times only for imported plain SRT."""
    start, end = float(caption["start"]), float(caption["end"])
    supplied = caption.get("words", [])
    result = []
    for item in supplied:
        try:
            item_start, item_end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        tokens = lao_tokens(item.get("text", ""))
        if tokens and item_end > item_start:
            weight, cursor = max(1, sum(len(token) for token in tokens)), item_start
            for index, token in enumerate(tokens):
                token_end = item_end if index == len(tokens) - 1 else cursor + (item_end - item_start) * len(token) / weight
                result.append({"start":cursor, "end":token_end, "text":token}); cursor = token_end
    if result: return result
    tokens = lao_tokens(caption.get("text", "")); total = max(1, sum(len(token) for token in tokens)); cursor = start
    for index, token in enumerate(tokens):
        token_end = end if index == len(tokens) - 1 else cursor + (end - start) * len(token) / total
        result.append({"start":cursor, "end":token_end, "text":token}); cursor = token_end
    return result

def preserve_caption_metadata(previous, incoming):
    """Preserve provider word timing across the browser's text-only save.

    The editor serializes rows as ``start|end|text``.  Without this merge, a
    save immediately before CapCut handoff discards ``words`` and the handoff
    has to invent per-word timing from character counts.  Metadata is copied
    only for an unchanged row; edited text or timing deliberately invalidates
    the old word clock.
    """
    result = copy.deepcopy(incoming)
    for index, caption in enumerate(result):
        if index >= len(previous):
            continue
        old = previous[index]
        try:
            unchanged = (
                str(caption.get("text", "")).strip() == str(old.get("text", "")).strip()
                and abs(float(caption.get("start", 0)) - float(old.get("start", 0))) <= .00001
                and abs(float(caption.get("end", 0)) - float(old.get("end", 0))) <= .00001
            )
        except (TypeError, ValueError):
            unchanged = False
        if not unchanged:
            continue
        for field in ("words", "speaker", "original_text"):
            if field in old and field not in caption:
                caption[field] = copy.deepcopy(old[field])
    return result

def validate_provider_word_timing(project):
    """Reject a provider transcript whose exact word clock was lost/corrupt."""
    trusted_sources = {"provider_word_timestamps", "provider_word_timestamps+lao_ctc", "whisperx_lao_ctc_forced_alignment"}
    if project.get("transcription", {}).get("timing_source") not in trusted_sources:
        return
    previous_end = -1.0
    for row_number, caption in enumerate(project.get("captions", []), 1):
        words = caption.get("words") or []
        if not words:
            raise RuntimeError(
                f"subtitle ແຖວ {row_number} ສູນເສຍ word timestamps; "
                "ກະລຸນາຖອດສຽງໃໝ່ກ່ອນສົ່ງເຂົ້າ CapCut."
            )
        joined = "".join(str(word.get("text", "")) for word in words)
        if joined != str(caption.get("text", "")):
            raise RuntimeError(
                f"subtitle ແຖວ {row_number} ຖືກແກ້ຂໍ້ຄວາມແຕ່ word timestamps ບໍ່ກົງ; "
                "ກະລຸນາຖອດສຽງ ຫຼືຈັບເວລາໃໝ່."
            )
        for word in words:
            try:
                start, end = float(word["start"]), float(word["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"word timestamp ແຖວ {row_number} ບໍ່ຖືກຕ້ອງ.") from exc
            if end <= start or start + .00001 < previous_end:
                raise RuntimeError(f"word timestamps ແຖວ {row_number} ຊ້ອນກັນ/ບໍ່ຮຽງຕາມເວລາ.")
            previous_end = end

def format_captions(captions, words_per_caption):
    """Create stable fixed-size rows from the complete chronological word list.

    Earlier versions restarted counting at every model chunk, which is why a
    3-word choice could visibly produce a mix of 3 and 4-ish rows.  The only
    permitted shorter row is the final remainder of the whole subtitle.
    """
    formatted = []
    limit = max(2, min(5, int(words_per_caption)))
    all_words = []
    for caption in captions:
        all_words.extend(timed_words(caption))
    all_words.sort(key=lambda word:(float(word["start"]), float(word["end"])))
    for offset in range(0, len(all_words), limit):
        group = all_words[offset:offset + limit]
        formatted.append({"start":round(group[0]["start"], 6), "end":round(group[-1]["end"], 6), "text":"".join(word["text"] for word in group), "words":[{"start":round(word["start"], 6), "end":round(word["end"], 6), "text":word["text"]} for word in group]})
    for index, caption in enumerate(formatted[:-1]):
        caption["end"] = round(min(caption["end"], formatted[index + 1]["start"] - .000001), 6)
    result = []
    for caption in formatted:
        if caption["end"] > caption["start"]:
            caption["words"][0]["start"] = caption["start"]; caption["words"][-1]["end"] = caption["end"]
            result.append(caption)
    return result

def constrain_captions_to_timeline(captions, timeline_end_us):
    """Keep subtitle and word boundaries inside the actual CapCut timeline.

    Different media readers can disagree by a few milliseconds at EOF.  A
    CapCut copy must never contain a text segment past its video timeline,
    otherwise the editor can drop that final subtitle.  This is a clamp, not
    a clock rescale: every earlier word keeps its measured timestamp.
    """
    limit = max(0.0, int(timeline_end_us) / 1000000.0)
    result = []
    for original in captions:
        caption = copy.deepcopy(original)
        start = min(limit, max(0.0, float(caption.get("start", 0))))
        end = min(limit, max(start, float(caption.get("end", start))))
        if end - start <= .000001: continue
        clipped_words = []
        for word in caption.get("words", []):
            word_start = min(end, max(start, float(word.get("start", start))))
            word_end = min(end, max(word_start, float(word.get("end", word_start))))
            if word_end - word_start > .000001:
                item = copy.deepcopy(word); item["start"] = round(word_start, 6); item["end"] = round(word_end, 6); clipped_words.append(item)
        caption["start"], caption["end"] = round(start, 6), round(end, 6)
        if clipped_words:
            clipped_words[0]["start"] = caption["start"]; clipped_words[-1]["end"] = caption["end"]
            caption["words"] = clipped_words
        result.append(caption)
    return result

def map_captions_to_capcut_timeline(captions, data):
    """Map source-video timestamps onto CapCut's cut/reordered timeline."""
    segments = [segment for track in data.get("tracks", []) if track.get("type") == "video" for segment in track.get("segments", [])]
    mappings = []
    for segment in segments:
        source = segment.get("source_timerange", {}); target = segment.get("target_timerange", {})
        ss, sd = float(source.get("start", 0)) / 1e6, float(source.get("duration", 0)) / 1e6
        ts, td = float(target.get("start", 0)) / 1e6, float(target.get("duration", 0)) / 1e6
        if sd > .000001 and td > .000001: mappings.append((ss, ss + sd, ts, td / sd))
    if not mappings: return captions
    caption_end = max((float(item.get("end", 0)) for item in captions), default=0.0)
    target_end = max((item[2] + (item[1] - item[0]) * item[3] for item in mappings), default=0.0)
    source_end = max((item[1] for item in mappings), default=0.0)
    # Some projects were already transcribed from the edited timeline.  Do
    # not remap those timestamps a second time.  In particular, a caption
    # clock that already covers most of the destination must never be sent
    # through the source->target cut map: that can collapse a 53s timeline to
    # the first 28s when the source file is longer than the edited video.
    if caption_end >= target_end * 0.85:
        return captions
    def convert(value):
        point = float(value)
        for start, end, target, scale in mappings:
            if start <= point <= end: return target + (point - start) * scale
        nearest = min(mappings, key=lambda item:min(abs(point - item[0]), abs(point - item[1])))
        return nearest[2] if point < nearest[0] else nearest[2] + (nearest[1] - nearest[0]) * nearest[3]
    mapped = []
    for caption in captions:
        item = copy.deepcopy(caption); words = item.get("words", [])
        if words:
            for word in words:
                word["start"], word["end"] = round(convert(word["start"]), 6), round(convert(word["end"]), 6)
            item["start"], item["end"] = words[0]["start"], words[-1]["end"]
        else:
            item["start"], item["end"] = round(convert(item["start"]), 6), round(convert(item["end"]), 6)
        if item["end"] > item["start"]: mapped.append(item)
    return sorted(mapped, key=lambda item:float(item["start"]))

def capcut_video_timeline_end(data, material_id=None):
    """Return the end of the copied video track in microseconds."""
    ends = []
    for track in data.get("tracks", []):
        if track.get("type") != "video":
            continue
        for segment in track.get("segments", []):
            if material_id and segment.get("material_id") != material_id:
                continue
            timerange = segment.get("target_timerange", {})
            try:
                ends.append(int(timerange.get("start", 0)) + int(timerange.get("duration", 0)))
            except (TypeError, ValueError):
                continue
    return max(ends, default=0)

def ensure_caption_coverage(captions, timeline_end_us):
    """Reject a handoff when the subtitle list is clearly truncated.

    A previous copy could contain a valid-looking count of captions while all
    captions stopped early (for example at 12s in a 49s video).  That is a
    source-data failure, not a CapCut rendering issue, so fail before writing
    the copy and tell the user exactly what must be regenerated.
    """
    if not captions:
        raise RuntimeError("subtitle ວ່າງ; ບໍ່ໄດ້ສ້າງ CapCut copy.")
    video_end = max(0.0, int(timeline_end_us) / 1000000.0)
    subtitle_end = max(float(item.get("end", 0.0)) for item in captions)
    tail_gap = video_end - subtitle_end
    # Allow a normal silent/outro tail, but never silently accept a large
    # truncated tail.  Short clips are exempt because a 2.5s outro is common.
    if video_end >= 8.0 and tail_gap > max(2.5, video_end * 0.35):
        raise RuntimeError(
            f"subtitle ບໍ່ຄົບ: subtitle ຈົບທີ່ {subtitle_end:.2f}s ແຕ່ video ຍາວ {video_end:.2f}s. "
            "ກະລຸນາຖອດສຽງ/ຈັດເວລາ subtitle ໃໝ່ກ່ອນສົ່ງເຂົ້າ CapCut."
        )

def snap_caption_edges(wav_path, captions):
    """Snap only a proven near-by silence edge to a subtitle boundary.

    Word interiors retain Gemini's word timestamps: audio amplitude cannot reliably
    distinguish consecutive Lao words when there is no pause between them.
    """
    if not captions: return captions
    try:
        import numpy as np
        import soundfile as sf
        samples, rate = sf.read(str(wav_path), always_2d=False)
        if getattr(samples, "ndim", 1) > 1: samples = samples.mean(axis=1)
        samples = np.asarray(samples, dtype=float)
        # Use every decoded sample, not 1-ms analysis blocks. At 48 kHz this
        # gives a 20.833 µs time grid; CapCut stores the result in µs.
        window = max(1, int(rate * .015))
        if len(samples) < window * 4: return captions
        power = samples * samples
        totals = np.concatenate(([0.0], np.cumsum(power)))
        envelope = np.sqrt((totals[window:] - totals[:-window]) / window)
        floor, peak = float(np.percentile(envelope, 18)), float(np.percentile(envelope, 99))
        threshold = max(0.0025, floor * 2.4, floor + (peak - floor) * .075)
        active = envelope >= threshold
        # Bridge natural sub-80 ms gaps within a syllable/word.
        gap_start = None
        for index, value in enumerate(active):
            if not value and gap_start is None: gap_start = index
            if value and gap_start is not None:
                if index - gap_start <= int(rate * .08): active[gap_start:index] = True
                gap_start = None
        spans, started = [], None
        for index, value in enumerate(active):
            if value and started is None: started = index
            if started is not None and (not value or index == len(active) - 1):
                finished = index if not value else index + 1
                if finished - started >= int(rate * .018): spans.append((started / rate, finished / rate))
                started = None
        for caption in captions:
            estimated_start, estimated_end = float(caption["start"]), float(caption["end"])
            # Amplitude can prove a local silence edge, but cannot distinguish
            # continuous Lao words.  The old broad 140 ms search moved a whole
            # subtitle to a neighbouring phrase.  Only accept a measured edge
            # inside a 35 ms guard band; word timing remains CTC/Gemini-led.
            possible_starts = [left for left, _ in spans if abs(left - estimated_start) <= .035]
            possible_ends = [right for _, right in spans if abs(right - estimated_end) <= .035]
            new_start = min(possible_starts, key=lambda point:abs(point - estimated_start)) if possible_starts else estimated_start
            new_end = min(possible_ends, key=lambda point:abs(point - estimated_end)) if possible_ends else estimated_end
            if new_end - new_start >= .08:
                caption["start"], caption["end"] = round(new_start, 6), round(new_end, 6)
                if caption.get("words"):
                    caption["words"][0]["start"] = caption["start"]; caption["words"][-1]["end"] = caption["end"]
        # Adjacent lines must never overlap, even where a continuous voice
        # region contains several short subtitle groups.
        for index, caption in enumerate(captions[:-1]):
            following = float(captions[index + 1]["start"])
            if float(caption["end"]) >= following:
                caption["end"] = round(max(float(caption["start"]) + .000001, following - .000001), 6)
                if caption.get("words"): caption["words"][-1]["end"] = caption["end"]
    except Exception:
        # Timing from Gemini remains valid when a particular media file cannot be decoded locally.
        pass
    return captions

def correct_caption_phase(wav_path, captions):
    """Correct a consistent voice/text phase lag without moving word content.

    CTC models can place every boundary a little late when the recording has
    a sharp onset.  Estimate that phase from many independent speech edges;
    never apply a correction from one word alone, and never exceed 180 ms.
    """
    if not captions: return captions
    try:
        import numpy as np, soundfile as sf
        samples, rate = sf.read(str(wav_path), always_2d=False)
        if getattr(samples, "ndim", 1) > 1: samples = samples.mean(axis=1)
        samples = np.asarray(samples, dtype=float); window = max(1, int(rate * .015))
        if len(samples) < window * 4: return captions
        power = samples * samples; totals = np.concatenate(([0.0], np.cumsum(power)));
        envelope = np.sqrt((totals[window:] - totals[:-window]) / window)
        floor, peak = float(np.percentile(envelope, 18)), float(np.percentile(envelope, 99))
        active = envelope >= max(.0025, floor * 2.4, floor + (peak - floor) * .075)
        spans, started = [], None
        for index, value in enumerate(active):
            if value and started is None: started = index
            if started is not None and (not value or index == len(active) - 1):
                finished = index if not value else index + 1
                if finished - started >= int(rate * .018): spans.append((started / rate, finished / rate))
                started = None
        starts, ends = [], []
        for caption in captions:
            start, end = float(caption["start"]), float(caption["end"])
            near_start = min(spans, key=lambda item: abs(item[0] - start), default=None)
            near_end = min(spans, key=lambda item: abs(item[1] - end), default=None)
            if near_start and abs(near_start[0] - start) <= .18: starts.append(near_start[0] - start)
            if near_end and abs(near_end[1] - end) <= .18: ends.append(near_end[1] - end)
        evidence = starts + ends
        if len(evidence) < 8: return captions
        shift = float(np.median(evidence))
        if abs(shift) < .035: return captions
        shift = max(-.18, min(.18, shift))
        for caption in captions:
            old_start, old_end = float(caption["start"]), float(caption["end"])
            caption["start"] = round(max(0.0, old_start + shift), 6)
            caption["end"] = round(max(caption["start"] + .012, old_end + shift), 6)
            for word in caption.get("words", []):
                word["start"] = round(max(0.0, float(word["start"]) + shift), 6)
                word["end"] = round(max(word["start"] + .012, float(word["end"]) + shift), 6)
        return captions
    except Exception:
        return captions

def force_align_lao_words(wav_path, words, phrase_words=3):
    """Locally forced-align known Lao words, with a confidence gate per phrase.

    This is the part of WhisperX that matters for a Lao transcript: a CTC
    acoustic model is forced to follow the *known* words, then its word edges
    are read back from the Viterbi path.  Unlike the former whole-video pass,
    each short phrase is aligned in its own time neighbourhood.  One bad word
    can therefore not pull every following subtitle earlier in the video.

    The model's vocabulary has no reliable Lao word-space token.  We retain
    the per-word character ownership while aligning and derive each word from
    the frames occupied by its characters.  A local greedy-decoding overlap
    check rejects a phrase when the audio does not support its supplied text.
    """
    global LAO_AUDITOR
    original_words = copy.deepcopy(words)
    if not (LAO_AUDITOR_DIR / "model.safetensors").is_file() or not words:
        return words
    try:
        import numpy as np
        import soundfile as sf
        import torch
        from transformers import AutoModelForCTC, AutoProcessor
        audio, rate = sf.read(str(wav_path), always_2d=False)
        if getattr(audio, "ndim", 1) > 1: audio = audio.mean(axis=1)
        duration = len(audio) / rate
        # Decode once.  Phrase-level Viterbi below is cheap and avoids global
        # drift when one Gemini phrase was missing or transcribed differently.
        if not .15 < duration <= 90: return words
        if LAO_AUDITOR is None:
            processor = AutoProcessor.from_pretrained(str(LAO_AUDITOR_DIR), local_files_only=True)
            model = AutoModelForCTC.from_pretrained(str(LAO_AUDITOR_DIR), local_files_only=True).eval()
            LAO_AUDITOR = (processor, model)
        processor, model = LAO_AUDITOR
        inputs = processor(audio, sampling_rate=rate, return_tensors="pt")
        with torch.no_grad(): emissions = torch.log_softmax(model(**inputs).logits[0], dim=-1).cpu().numpy()
        total_frames = emissions.shape[0]
        seconds_per_frame = duration / total_frames
        vocabulary, blank = processor.tokenizer.get_vocab(), processor.tokenizer.pad_token_id

        def target_ids(first, last):
            targets, owners = [], []
            for index in range(first, last):
                chars = [vocabulary.get(char) for char in str(words[index].get("text", ""))]
                # Never align a partially representable word: that creates a
                # convincing but false edge for a word the acoustic model lacks.
                if not chars or any(token is None for token in chars): return [], []
                targets.extend(chars); owners.extend([index - first] * len(chars))
            return targets, owners

        def align_phrase(first, last, padding):
            targets, owners = target_ids(first, last)
            if not targets or len(targets) > 180: return None
            left_time = max(0.0, float(words[first]["start"]) - padding)
            right_time = min(duration, float(words[last - 1]["end"]) + padding)
            left = max(0, int(left_time / seconds_per_frame))
            right = min(total_frames, max(left + 2, int(np.ceil(right_time / seconds_per_frame))))
            local = emissions[left:right]
            if len(local) < len(targets): return None
            states = np.empty(len(targets) * 2 + 1, dtype=np.int32)
            states[0::2] = blank; states[1::2] = np.asarray(targets, dtype=np.int32)
            previous = np.full(len(states), -np.inf, dtype=np.float32)
            previous[0] = local[0, blank]
            if len(states) > 1: previous[1] = local[0, states[1]]
            back = np.full((len(local), len(states)), -1, dtype=np.int32)
            for frame in range(1, len(local)):
                current = np.full(len(states), -np.inf, dtype=np.float32)
                for state_index in range(len(states)):
                    best, score = state_index, previous[state_index]
                    if state_index and previous[state_index - 1] > score: best, score = state_index - 1, previous[state_index - 1]
                    if state_index > 1 and states[state_index] != blank and states[state_index] != states[state_index - 2] and previous[state_index - 2] > score: best, score = state_index - 2, previous[state_index - 2]
                    current[state_index] = score + local[frame, states[state_index]]
                    back[frame, state_index] = best
                previous = current
            state = len(states) - 1 if previous[-1] >= previous[-2] else len(states) - 2
            path = np.empty(len(local), dtype=np.int32)
            for frame in range(len(local) - 1, -1, -1):
                path[frame] = state; state = back[frame, state] if frame else state
            matched, scores = [[] for _ in range(last - first)], [[] for _ in range(last - first)]
            for frame, state_index in enumerate(path):
                if state_index % 2:
                    owner = owners[(state_index - 1) // 2]
                    matched[owner].append(left + frame); scores[owner].append(float(local[frame, states[state_index]]))
            if any(not frames for frames in matched): return None
            # Compare model-decoded characters with the expected phrase.  The
            # denominator is the supplied phrase (not the padded context), so
            # neighbours before/after a phrase do not unfairly reduce trust.
            greedy = local.argmax(axis=1).tolist(); collapsed = []
            for token in greedy:
                if token != blank and (not collapsed or token != collapsed[-1]): collapsed.append(token)
            overlap = sum(size for _, _, size in SequenceMatcher(a=targets, b=collapsed, autojunk=False).get_matching_blocks()) / len(targets)
            confidence = float(np.mean([score for group in scores for score in group]))
            output = []
            for index, frames in enumerate(matched):
                output.append(((min(frames) * seconds_per_frame), ((max(frames) + 1) * seconds_per_frame)))
            # A genuine phrase correction moves its words together.  If only
            # one word is pulled to the edge of a padded window, the supplied
            # text and local audio disagree, so retain Gemini's time instead.
            shifts = [start - float(words[index]["start"]) for index, (start, _) in enumerate(output, first)]
            center = float(np.median(shifts))
            duration_ok = all(
                (end - start) <= max(.28, (float(words[index]["end"]) - float(words[index]["start"])) * 2.2)
                for index, (start, end) in enumerate(output, first)
            )
            # Lao recordings with product names and Thai loanwords produce a
            # lower greedy-character overlap even when the Viterbi path has a
            # clear acoustic location.  Retain confidence and local-coherence
            # gates, but do not reject most real words solely for imperfect
            # spelling agreement.  The wider limits remain bounded inside the
            # phrase's 350 ms neighbourhood.
            if overlap < .32 or confidence < -7.0 or not duration_ok or max(abs(shift - center) for shift in shifts) > .30:
                return None
            return overlap, confidence, output

        # Groups are short, timestamp-anchored phrases.  This preserves the
        # relationship to Gemini/VAD while permitting a substantial correction
        # when the acoustic evidence is clear.
        blocks, first = [], 0
        for index in range(1, len(words) + 1):
            if index == len(words):
                blocks.append((first, index)); break
            gap = float(words[index]["start"]) - float(words[index - 1]["end"])
            span = float(words[index]["end"]) - float(words[first]["start"])
            if index - first >= max(2, min(5, int(phrase_words))) or gap >= .38 or span >= 2.8:
                blocks.append((first, index)); first = index
        aligned = [dict(word) for word in words]
        for first, last in blocks:
            # Never let a wider padded window beat a valid tight alignment.
            # In continuous speech the wider window often contains the same
            # syllable in a neighbouring phrase and therefore produced an
            # apparently high-confidence, but visibly early, highlight.  The
            # 850 ms window is now fallback-only when the 350 ms acoustic
            # neighbourhood cannot align the supplied phrase at all.
            candidate = align_phrase(first, last, .35)
            if candidate is None:
                candidate = align_phrase(first, last, .85)
            if candidate is None: continue
            _, _, timings = candidate
            for index, (start, end) in enumerate(timings, first):
                aligned[index] = {"start":round(start, 6), "end":round(max(start + .012, end), 6), "text":words[index]["text"]}
        # Enforce strict order, a requirement for SRT and CapCut karaoke.
        # Preserve the word's measured duration when an accepted phrase moves
        # across a neighbouring rejected phrase.  The former 12 ms clipping
        # created visibly premature colour changes for those rejected words.
        for index in range(1, len(aligned)):
            previous_end = float(aligned[index - 1]["end"])
            if float(aligned[index]["start"]) < previous_end:
                # A rejected provider interval may itself be much too long.
                # Retain a visible 40–120 ms window, not an untrusted full
                # duration that would push every following word forward.
                measured_duration = max(.04, min(.12, float(aligned[index]["end"]) - float(aligned[index]["start"])))
                aligned[index]["start"] = round(previous_end, 6)
                aligned[index]["end"] = round(min(duration, max(previous_end + measured_duration, float(aligned[index]["end"]))), 6)
                if aligned[index]["end"] <= aligned[index]["start"]:
                    aligned[index]["end"] = round(min(duration, aligned[index]["start"] + .012), 6)
        if any(
            float(word.get("end", 0)) <= float(word.get("start", 0))
            or float(word.get("start", 0)) < 0
            or float(word.get("end", 0)) > duration + .000001
            for word in aligned
        ):
            return original_words
        return aligned
    except Exception:
        return words

def whisperx_force_align_lao_words(wav_path, words):
    """Globally force the complete known Lao transcript onto the source audio.

    Lao normally has no spaces, so WhisperX sees each subtitle row as one
    token.  Supplying the already-tokenized JodSub words separated by spaces
    makes WhisperX return one independently aligned window per displayed word.
    A single full-audio segment also avoids the former provider-window trap:
    an early Gemini boundary can no longer prevent the aligner from finding a
    later acoustic occurrence.
    """
    global WHISPERX_LAO_ALIGNER
    if not words or not (LAO_AUDITOR_DIR / "model.safetensors").is_file():
        return []
    try:
        import soundfile as sf
        import whisperx
        duration = sf.info(str(wav_path)).frames / sf.info(str(wav_path)).samplerate
        if not .15 < duration <= 180:
            return []
        if WHISPERX_LAO_ALIGNER is None:
            WHISPERX_LAO_ALIGNER = whisperx.load_align_model(
                language_code="lo", device="cpu", model_name=str(LAO_AUDITOR_DIR)
            )
        model, metadata = WHISPERX_LAO_ALIGNER
        transcript = " ".join(str(word.get("text", "")).strip() for word in words)
        # WhisperX word segments can collapse a short Lao word to a tiny
        # interval even when its character alignment spans the spoken vowel.
        # Build each known word from its actual Lao characters; this removes
        # the visible colour flicker while keeping subtitle and karaoke clocks
        # identical.
        aligned_result = whisperx.align(
            [{"start":0.0, "end":duration, "text":transcript}],
            model, metadata, str(wav_path), "cpu", return_char_alignments=True,
        )
        chars = aligned_result.get("segments", [{}])[0].get("chars", [])
        aligned = []
        cursor = 0
        if chars:
            for expected in words:
                target = [char for char in str(expected.get("text", "")) if not char.isspace()]
                picked = []
                while cursor < len(chars) and len(picked) < len(target):
                    item = chars[cursor]; cursor += 1
                    if str(item.get("char", "")).isspace(): continue
                    if str(item.get("char", "")) != target[len(picked)]: return []
                    picked.append(item)
                if len(picked) != len(target): return []
                aligned.append({"word":expected["text"], "start":picked[0].get("start"), "end":picked[-1].get("end")})
        else:
            aligned = aligned_result.get("word_segments", [])
        if len(aligned) != len(words): return []
        result = []
        previous_end = 0.0
        for index, (expected, heard) in enumerate(zip(words, aligned)):
            if str(heard.get("word", "")).strip() != str(expected.get("text", "")).strip():
                return []
            start = max(previous_end, min(duration, float(heard["start"])))
            # Karaoke should change colour at the next word's acoustic onset,
            # not at WhisperX's very short character end.  Lao syllables often
            # have a brief/uncertain end boundary; using the next onset keeps
            # the active word stable through that gap without changing the
            # ordering or making the subtitle lead the speech.
            if index + 1 < len(aligned):
                next_start = max(start, min(duration, float(aligned[index + 1]["start"])))
                end = next_start
            else:
                end = duration
            end = min(duration, max(start + .001, end))
            if end <= start:
                return []
            result.append({"start":round(start, 6), "end":round(end, 6), "text":str(expected["text"])})
            previous_end = end
        return result
    except Exception:
        return []

def openai_whisper_v3_ready():
    """A partial resumable download must never be loaded as a model."""
    return OPENAI_WHISPER_V3_FILE.is_file() and OPENAI_WHISPER_V3_FILE.stat().st_size == OPENAI_WHISPER_V3_SIZE

def whisperx_v3_ready():
    """WhisperX requires its CTranslate2 model files, separate from .pt."""
    if not ((WHISPERX_V3_DIR / "config.json").is_file() and any(WHISPERX_V3_DIR.glob("model*.bin"))):
        return False
    # The CTranslate2 files alone are not enough: WhisperX's VAD wrapper also
    # needs pyannote.  Report false on a clean machine so the app takes the
    # normal Lao CTC/Gemini path instead of repeatedly starting a doomed pass.
    try:
        import importlib.util
        return importlib.util.find_spec("whisperx") is not None and importlib.util.find_spec("pyannote") is not None
    except Exception:
        return False

def whisperx_v3_lao_word_times(wav_path):
    """Actual WhisperX word alignment: Large-v3 transcript + Lao CTC aligner.

    WhisperX has no bundled Lao phoneme model, so the local SiangLao XLS-R CTC
    checkpoint is explicitly supplied as its language-specific alignment model.
    It returns word windows only; Gemini text is never silently overwritten.
    """
    global WHISPERX_V3
    if not whisperx_v3_ready() or not (LAO_AUDITOR_DIR / "model.safetensors").is_file(): return []
    try:
        import whisperx
        if WHISPERX_V3 is None:
            WHISPERX_V3 = whisperx.load_model(str(WHISPERX_V3_DIR), "cpu", compute_type="int8", language="lo")
        output = WHISPERX_V3.transcribe(str(wav_path), batch_size=1)
        align_model, metadata = whisperx.load_align_model(language_code="lo", device="cpu", model_name=str(LAO_AUDITOR_DIR))
        output = whisperx.align(output.get("segments", []), align_model, metadata, str(wav_path), "cpu", return_char_alignments=False)
        words = []
        for segment in output.get("segments", []):
            for item in segment.get("words", []):
                text = "".join(char for char in str(item.get("word", "")) if "\u0e80" <= char <= "\u0eff")
                if text and float(item.get("end", 0)) > float(item.get("start", 0)):
                    words.append({"start":float(item["start"]), "end":float(item["end"]), "text":text})
        return words
    except Exception:
        # The existing CTC/Gemini timing is a safe local fallback if Large-v3
        # cannot run on a particular CPU or a recording is malformed.
        return []

def whisperx_diarized_captions(video, hf_token, words_per_caption, fast=False):
    """Transcribe and attach Speaker labels with WhisperX diarization."""
    if not whisperx_v3_ready():
        raise RuntimeError("ຍັງບໍ່ພົບ WhisperX + pyannote ໃນເຄື່ອງ; ຕ້ອງຕິດຕັ້ງໂມເດວກ່ອນ.")
    if not str(hf_token or '').strip():
        raise RuntimeError("ກະລຸນາໃສ່ Hugging Face token ສຳລັບ pyannote ກ່ອນ.")
    import soundfile as sf
    import whisperx
    token = uuid.uuid4().hex
    wav = WORK / f"jodsub-diarize-{token}.wav"
    try:
        subprocess.run([ffmpeg(), "-y", "-i", str(video), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        STATUS.update({"message":f"ກຳລັງຖອດສຽງແຍກຄົນເວົ້າ{'ໂໝດໄວ' if fast else 'ຄວາມແມ່ນຍຳສູງ'} (45%)", "progress":45})
        global WHISPERX_V3
        if WHISPERX_V3 is None:
            WHISPERX_V3 = whisperx.load_model(str(WHISPERX_V3_DIR), "cpu", compute_type="int8", language="lo")
        result = WHISPERX_V3.transcribe(str(wav), batch_size=1)
        if not fast and (LAO_AUDITOR_DIR / "model.safetensors").is_file():
            align_model, metadata = whisperx.load_align_model(language_code="lo", device="cpu", model_name=str(LAO_AUDITOR_DIR))
            result = whisperx.align(result.get("segments", []), align_model, metadata, str(wav), "cpu", return_char_alignments=False)
        STATUS.update({"message":"ກຳລັງແຍກ Speaker 1, Speaker 2… (75%)", "progress":75})
        from whisperx.diarize import DiarizationPipeline
        diarizer = DiarizationPipeline(use_auth_token=str(hf_token).strip(), device="cpu")
        diarization = diarizer(str(wav))
        result = whisperx.assign_word_speakers(diarization, result)
        limit = max(2, min(5, int(words_per_caption)))
        words = []
        for segment in result.get("segments", []):
            speaker = str(segment.get("speaker") or "SPEAKER_00")
            items = segment.get("words", [])
            if not items:
                # Fast mode skips forced alignment; distribute the segment text
                # over its known segment clock so no spoken text is discarded.
                tokens = lao_tokens(segment.get("text", ""))
                a, b = float(segment.get("start", 0)), float(segment.get("end", 0))
                total = max(1, sum(len(token) for token in tokens)); cursor = a
                items = []
                for index, token_text in enumerate(tokens):
                    token_end = b if index == len(tokens) - 1 else cursor + (b - a) * len(token_text) / total
                    items.append({"start":cursor, "end":token_end, "word":token_text}); cursor = token_end
            for item in items:
                text = str(item.get("word", item.get("text", ""))).strip()
                start, end = float(item.get("start", segment.get("start", 0))), float(item.get("end", segment.get("end", 0)))
                if text and end > start:
                    words.append({"start":start, "end":end, "text":text, "speaker":speaker})
        if not words:
            raise RuntimeError("WhisperX ບໍ່ສົ່ງ word timestamps ສຳລັບສຽງນີ້.")
        captions = []
        cursor = 0
        while cursor < len(words):
            speaker = words[cursor]["speaker"]
            group = []
            # Never combine adjacent words from different speakers in one row,
            # and advance only by words actually consumed (no dropped tail when
            # a speaker changes inside the normal row size).
            while cursor < len(words) and len(group) < limit and words[cursor]["speaker"] == speaker:
                group.append(words[cursor]); cursor += 1
            label = speaker.replace("SPEAKER_", "Speaker ")
            captions.append({"start":group[0]["start"], "end":group[-1]["end"], "text":"".join(item["text"] for item in group), "speaker":label, "words":[{k:v for k,v in item.items() if k != "speaker"} for item in group]})
        return captions
    finally:
        wav.unlink(missing_ok=True)

def assemblyai_diarized_captions(video, api_key, words_per_caption):
    """Optional cloud-only speaker diarization; does not alter normal ASR."""
    key = str(api_key or '').strip()
    if not key: raise RuntimeError("ກະລຸນາໃສ່ AssemblyAI API key ກ່ອນ.")
    wav = WORK / f"jodsub-assembly-{uuid.uuid4().hex}.wav"
    try:
        subprocess.run([ffmpeg(), "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        audio = wav.read_bytes()
        req = urlrequest.Request("https://api.assemblyai.com/v2/upload", data=audio, headers={"authorization": key, "content-type": "application/octet-stream"}, method="POST")
        with urlopen_retry(req, timeout=120) as resp: upload = json.loads(resp.read().decode())
        source = upload.get("upload_url")
        if not source: raise RuntimeError("AssemblyAI ບໍ່ສົ່ງ upload URL.")
        body = json.dumps({"audio_url": source, "speaker_labels": True, "language_code": "lo", "punctuate": True, "format_text": True}).encode()
        req = urlrequest.Request("https://api.assemblyai.com/v2/transcript", data=body, headers={"authorization": key, "content-type": "application/json"}, method="POST")
        with urlopen_retry(req, timeout=120) as resp: job = json.loads(resp.read().decode())
        job_id = job.get("id")
        if not job_id: raise RuntimeError("AssemblyAI ບໍ່ສ້າງວຽກຖອດສຽງ.")
        for _ in range(180):
            time.sleep(2)
            req = urlrequest.Request(f"https://api.assemblyai.com/v2/transcript/{job_id}", headers={"authorization": key})
            with urlopen_retry(req, timeout=60) as resp: result = json.loads(resp.read().decode())
            if result.get("status") == "completed": break
            if result.get("status") == "error": raise RuntimeError(f"AssemblyAI: {result.get('error', 'ປະມວນຜົນບໍ່ສຳເລັດ')}")
        else: raise RuntimeError("AssemblyAI ໃຊ້ເວລາດົນເກີນໄປ.")
        words = [{"start": float(w.get("start", 0))/1000, "end": float(w.get("end", 0))/1000, "text": str(w.get("text", "")).strip(), "speaker": str(w.get("speaker", "A"))} for w in result.get("words", []) if str(w.get("text", "")).strip()]
        if not words: raise RuntimeError("AssemblyAI ບໍ່ພົບຄຳເວົ້າ.")
        # Lao is normally written without spaces, so some cloud responses
        # return one long token for an entire utterance.  Reconstruct stable
        # time windows from utterances instead of producing one huge subtitle.
        if result.get("utterances"):
            rebuilt = []
            for utterance in result.get("utterances", []):
                text = str(utterance.get("text", "")).strip()
                if not text: continue
                start, end = float(utterance.get("start", 0))/1000, float(utterance.get("end", 0))/1000
                tokens = lao_tokens(text)
                if len(tokens) == 1:
                    compact = re.sub(r"\s+", "", text)
                    tokens = [compact[i:i+10] for i in range(0, len(compact), 10)]
                total = max(1, sum(len(t) for t in tokens)); cursor_time = start
                speaker = str(utterance.get("speaker", "A"))
                for index, token_text in enumerate(tokens):
                    token_end = end if index == len(tokens)-1 else cursor_time + (end-start)*len(token_text)/total
                    rebuilt.append({"start":cursor_time, "end":token_end, "text":token_text, "speaker":speaker}); cursor_time = token_end
            if rebuilt: words = rebuilt
        limit = max(1, int(words_per_caption or 3)); captions = []; cursor = 0
        while cursor < len(words):
            speaker = words[cursor]["speaker"]; group = []
            while cursor < len(words) and len(group) < limit and words[cursor]["speaker"] == speaker:
                group.append(words[cursor]); cursor += 1
            captions.append({"start": group[0]["start"], "end": group[-1]["end"], "text": "".join(x["text"] for x in group), "speaker": f"ຄົນເວົ້າ {speaker}", "words": [{k:v for k,v in x.items() if k != "speaker"} for x in group]})
        return captions
    except urlerror.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"AssemblyAI HTTP {exc.code}: {detail}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"AssemblyAI ເຊື່ອມຕໍ່ບໍ່ສຳເລັດ: {exc.reason}") from exc
    finally:
        try: wav.unlink()
        except OSError: pass

def whisper_v3_lao_word_times(wav_path):
    """Fallback OpenAI Large-v3 timing while WhisperX's model download runs."""
    global OPENAI_WHISPER_V3
    if not openai_whisper_v3_ready(): return []
    try:
        import soundfile as sf
        import whisper
        if OPENAI_WHISPER_V3 is None:
            OPENAI_WHISPER_V3 = whisper.load_model("large-v3", device="cpu", download_root=str(OPENAI_WHISPER_V3_DIR))
        # Pass decoded samples directly: OpenAI Whisper otherwise calls a
        # system `ffmpeg`, while this app deliberately uses its bundled binary.
        audio, rate = sf.read(str(wav_path), always_2d=False, dtype="float32")
        if getattr(audio, "ndim", 1) > 1: audio = audio.mean(axis=1)
        if rate != 16000: return []
        output = OPENAI_WHISPER_V3.transcribe(audio, language="lo", task="transcribe", word_timestamps=True, fp16=False, temperature=0, condition_on_previous_text=False, verbose=False)
        words = []
        for segment in output.get("segments", []):
            for item in segment.get("words", []):
                text = "".join(char for char in str(item.get("word", "")) if "\u0e80" <= char <= "\u0eff")
                if text and float(item.get("end", 0)) > float(item.get("start", 0)):
                    words.append({"start":float(item["start"]), "end":float(item["end"]), "text":text})
        return words
    except Exception:
        return []

def refine_gemini_words_with_whisper_v3(wav_path, words):
    """Apply Whisper timestamps only when it independently recognises the word."""
    # Prefer the installed WhisperX pass.  Do not fall through to the much
    # slower OpenAI .pt checkpoint after WhisperX returns no Lao words: the
    # base multilingual checkpoint was tested on Lao audio and produced Thai
    # script, so using it would add latency without improving alignment.
    if whisperx_v3_ready():
        heard = whisperx_v3_lao_word_times(wav_path)
        if not heard:
            STATUS.update({"message":"WhisperX ບໍ່ໄດ້ຄຳພາສາລາວທີ່ໃຊ້ໄດ້ — ກັບໄປ Lao CTC ຢ່າງປອດໄພ (90%)", "progress":90})
    else:
        heard = whisper_v3_lao_word_times(wav_path)
    if not heard: return words
    aligned, cursor = [], 0
    for word in words:
        text = str(word.get("text", ""))
        old_start, old_end = float(word["start"]), float(word["end"])
        match_index = next((index for index in range(cursor, min(len(heard), cursor + 8)) if heard[index]["text"] == text), None)
        if match_index is not None:
            candidate = heard[match_index]
            # Large-v3 must agree linguistically and be in the same local
            # utterance. This guards against repeated words such as ແລະ/ບໍ່.
            if abs(candidate["start"] - old_start) <= .75 and abs(candidate["end"] - old_end) <= .75:
                old_start, old_end = candidate["start"], candidate["end"]
                cursor = match_index + 1
        aligned.append({"start":round(old_start, 6), "end":round(max(old_start + .012, old_end), 6), "text":text})
    return aligned

def detect_dead_air(video_path, minimum_gap=.55, padding=.09, threshold_db=None, pre_roll=None, post_roll=None):
    """Return source-time ranges to keep after removing only clear, long silence.

    This mirrors a non-destructive editor auto-cut: the media file is never
    rendered or overwritten.  The ranges are later represented as ordinary,
    individually editable CapCut video segments.
    """
    source, wav = Path(video_path), WORK / f"dead-air-{uuid.uuid4().hex}.wav"
    try:
        subprocess.run([ffmpeg(), "-y", "-i", str(source), "-vn", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        import numpy as np
        import soundfile as sf
        samples, rate = sf.read(str(wav), always_2d=False)
        if getattr(samples, "ndim", 1) > 1: samples = samples.mean(axis=1)
        samples = np.asarray(samples, dtype=float); duration = len(samples) / rate
        step = max(1, int(rate / 100))  # 10 ms analysis avoids false cuts in syllables.
        usable = len(samples) - len(samples) % step
        if usable < step * 20: return [(0.0, duration)], []
        rms = np.sqrt(np.mean(samples[:usable].reshape(-1, step) ** 2, axis=1))
        envelope = np.convolve(rms, np.ones(5) / 5, mode="same")
        floor, peak = float(np.percentile(envelope, 18)), float(np.percentile(envelope, 99))
        if threshold_db is not None:
            # A learned dB value is used only after multi-project evidence has
            # enabled the profile.  It is converted to the same amplitude
            # scale as the measured RMS envelope.
            threshold = 10 ** (float(threshold_db) / 20)
        else:
            threshold = max(.0025, floor * 2.4, floor + (peak - floor) * .075)
        active = envelope >= threshold
        # Do not split a word merely because it has a very short acoustic gap.
        start = None
        for index, value in enumerate(active):
            if not value and start is None: start = index
            if value and start is not None:
                if index - start <= 8: active[start:index] = True
                start = None
        quiet, started = [], None
        for index, value in enumerate(active):
            if not value and started is None: started = index
            if started is not None and (value or index == len(active) - 1):
                finished = index if value else index + 1
                a, b = started / 100, finished / 100
                if b - a >= minimum_gap and a > .04 and b < duration - .04:
                    # Retain the measured post-roll after the preceding word
                    # and pre-roll before the next word.  Old projects still
                    # use the symmetric padding value.
                    after_previous = padding if post_roll is None else float(post_roll)
                    before_next = padding if pre_roll is None else float(pre_roll)
                    cut_a, cut_b = a + after_previous, b - before_next
                    if cut_b - cut_a >= .10: quiet.append((cut_a, cut_b))
                started = None
        if not quiet: return [(0.0, duration)], []
        kept, cursor = [], 0.0
        for cut_a, cut_b in quiet:
            if cut_a - cursor >= .04: kept.append((cursor, cut_a))
            cursor = cut_b
        if duration - cursor >= .04: kept.append((cursor, duration))
        return kept or [(0.0, duration)], quiet
    except Exception as exc:
        raise RuntimeError(f"ກວດ Dead Air ບໍ່ໄດ້: {str(exc)[:180]}") from exc
    finally:
        wav.unlink(missing_ok=True)

def primary_video_segment(data):
    materials = data.get("materials", {}).get("videos", [])
    by_id = {item.get("id"):item for item in materials}
    candidates = []
    for track in data.get("tracks", []):
        if track.get("type") != "video": continue
        for index, segment in enumerate(track.get("segments", [])):
            material = by_id.get(segment.get("material_id"))
            if material:
                duration = segment.get("target_timerange", {}).get("duration", 0)
                candidates.append((duration, track, index, segment, material))
    if not candidates: raise RuntimeError("CapCut project ບໍ່ມີ video segment ໃຫ້ຕັດ Dead Air.")
    return max(candidates, key=lambda item:item[0])

def enabled_config_flag(value):
    """Interpret persisted checkbox values without treating ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return False

def apply_dead_air_cuts(data, project_video, config):
    """Split the main video into editable CapCut shots and return its time map."""
    _, track, segment_index, original, material = primary_video_segment(data)
    source_path = Path(material.get("path") or project_video)
    if not source_path.is_file(): source_path = Path(project_video)
    if not source_path.is_file(): raise RuntimeError("ບໍ່ພົບ video ສຳລັບວິເຄາະ Dead Air.")
    minimum_gap = max(.30, min(3.0, float(config.get("deadAirGap", .55))))
    padding = max(.03, min(.30, float(config.get("deadAirPadding", .09))))
    threshold_db = pre_roll = post_roll = None
    if enabled_config_flag(config.get("useEditingStyle")):
        profile = load_editing_profile(EDITING_STYLE_PROFILE)
        silence = profile.get("silence", {})
        if profile.get("automation_enabled") and silence.get("threshold_db") is not None:
            minimum_gap = max(.30, min(3.0, float(silence.get("minimum_ms", minimum_gap * 1000)) / 1000))
            threshold_db = float(silence["threshold_db"])
            pre_roll = max(.03, min(.30, float(silence.get("pre_roll_ms") or padding * 1000) / 1000))
            post_roll = max(.03, min(.30, float(silence.get("post_roll_ms") or padding * 1000) / 1000))
    kept, quiet = detect_dead_air(source_path, minimum_gap, padding, threshold_db, pre_roll, post_roll)
    source_range = original.get("source_timerange") or {"start":0, "duration":int(media_duration(source_path))}
    target_range = original.get("target_timerange") or {"start":0, "duration":source_range["duration"]}
    source_start = source_range["start"] / 1000000; source_end = source_start + source_range["duration"] / 1000000
    target_start = target_range["start"] / 1000000
    speed = max(.01, float(original.get("speed", 1.0)))
    kept = [(max(start, source_start), min(end, source_end)) for start, end in kept if min(end, source_end) - max(start, source_start) >= .04]
    if len(kept) <= 1 and not quiet: return None
    original_kept = [(target_start + (start - source_start) / speed, target_start + (end - source_start) / speed) for start, end in kept]
    cursor = target_start; shots = []
    for source_a, source_b in kept:
        shot = copy.deepcopy(original); shot["id"] = str(uuid.uuid4()).upper()
        shot["source_timerange"] = {"start":int(source_a * 1000000), "duration":max(40000, int((source_b - source_a) * 1000000))}
        duration = max(40000, int((source_b - source_a) / speed * 1000000))
        shot["target_timerange"] = {"start":int(cursor * 1000000), "duration":duration}; shots.append(shot); cursor += duration / 1000000
    track["segments"][segment_index:segment_index + 1] = shots
    removed = [(left, right) for left, right in zip([target_start] + [item[1] for item in original_kept], [item[0] for item in original_kept] + [target_start + target_range["duration"] / 1000000]) if right - left >= .04]
    def remap(value):
        value = float(value); removed_before = sum(max(0.0, min(value, end) - start) for start, end in removed)
        return round(value - removed_before, 3)
    return {"kept":original_kept, "quiet":quiet, "shots":len(shots), "removed":removed, "remap":remap, "video_material_id":material["id"]}

def captions_after_cuts(captions, cut):
    if not cut: return copy.deepcopy(captions)
    remapped = []
    for caption in captions:
        for left, right in cut["kept"]:
            start, end = max(float(caption["start"]), left), min(float(caption["end"]), right)
            if end - start < .035: continue
            words = []
            for word in timed_words(caption):
                word_start, word_end = max(float(word["start"]), left), min(float(word["end"]), right)
                if word_end - word_start >= .015: words.append({"start":cut["remap"](word_start), "end":cut["remap"](word_end), "text":word["text"]})
            text = "".join(word["text"] for word in words) if words else str(caption["text"])
            remapped.append({"start":cut["remap"](start), "end":cut["remap"](end), "text":text, "words":words})
    return [caption for caption in remapped if caption["end"] > caption["start"]]

def calibrate_word_clock(words, audio_duration):
    """Correct the occasional Gemini clock drift before acoustic alignment.

    Gemini can return valid-looking timestamps whose final edge is beyond the
    audio's real duration.  That is a clock-scale error, not an edit decision:
    for example, this project's final word was 50.000 seconds although its
    source audio is only 46.647 seconds.  Scale only this narrow, detectable
    failure mode.  A much larger mismatch is rejected rather than compressed.
    """
    if not words or audio_duration <= .15: return copy.deepcopy(words), 1.0
    reported_end = max(float(word.get("end", 0)) for word in words)
    if reported_end <= audio_duration + .20: return copy.deepcopy(words), 1.0
    scale = audio_duration / reported_end
    if not .84 <= scale < .996:
        raise RuntimeError(f"ເວລາ subtitle ({reported_end:.3f}s) ບໍ່ກົງກັບສຽງ ({audio_duration:.3f}s) ຫຼາຍເກີນໄປ; ບໍ່ໄດ້ບີບເວລາແບບຄາດເດົາ.")
    calibrated = []
    for word in words:
        start, end = float(word["start"]) * scale, float(word["end"]) * scale
        calibrated.append({"start":round(start, 6), "end":round(max(start + .012, end), 6), "text":str(word["text"])})
    return calibrated, scale

def local_realign_captions(video_path, captions, words_per_caption):
    """Re-time an existing project against its exact source audio, locally."""
    source = Path(video_path)
    if not source.is_file(): raise RuntimeError("ບໍ່ພົບວິດີໂອຕົ້ນສະບັບສຳລັບຈັບເວລາ.")
    words = [word for caption in captions for word in timed_words(caption)]
    if not words: raise RuntimeError("ຍັງບໍ່ມີ subtitle ໃຫ້ຈັບເວລາ.")
    wav = WORK / f"jodsub-realign-{uuid.uuid4().hex}.wav"
    try:
        subprocess.run([ffmpeg(), "-y", "-i", str(source), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        import soundfile as sf
        info = sf.info(str(wav)); audio_duration = info.frames / info.samplerate
        words, scale = calibrate_word_clock(words, audio_duration)
        STATUS.update({"message":"ກຳລັງຈັບເວລາແຕ່ລະຄຳຈາກສຽງຕົ້ນສະບັບ (78%)", "progress":78})
        whisperx_words = whisperx_force_align_lao_words(wav, words)
        words = whisperx_words or force_align_lao_words(wav, words, words_per_caption)
        aligned = []
        for index in range(0, len(words), words_per_caption):
            group = words[index:index + words_per_caption]
            aligned.append({"start":group[0]["start"], "end":group[-1]["end"], "text":"".join(item["text"] for item in group), "words":copy.deepcopy(group)})
        aligned = correct_caption_phase(wav, aligned)
        aligned = snap_caption_edges(wav, aligned)
        return format_captions(aligned, words_per_caption), scale
    finally:
        wav.unlink(missing_ok=True)

def sfx_after_cuts(events, cut):
    if not cut: return copy.deepcopy(events)
    remapped = []
    for event in events:
        time_value = float(event["time"])
        if any(start <= time_value <= end for start, end in cut["kept"]):
            item = copy.deepcopy(event); item["time"] = cut["remap"](time_value); remapped.append(item)
    return remapped

def gemini_models(api_key):
    """List precisely the models this key may call; no model list is hard-coded."""
    if not api_key: raise RuntimeError("ກະລຸນາໃສ່ Gemini API key ກ່ອນກົດໂຫຼດ model.")
    models, page_token = [], ""
    while True:
        query = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
        if page_token: query += "&pageToken=" + page_token
        request = urlrequest.Request(query, headers={"x-goog-api-key":api_key}, method="GET")
        try:
            with urlopen_retry(request, timeout=30) as response: body = json.loads(response.read().decode())
        except urlerror.HTTPError as exc: raise RuntimeError(f"ອ່ານ Gemini model ບໍ່ໄດ້ (HTTP {exc.code}).") from exc
        models.extend(item for item in body.get("models", []) if "generateContent" in item.get("supportedGenerationMethods", []) and item.get("name", "").startswith("models/gemini-"))
        page_token = body.get("nextPageToken", "")
        if not page_token: break
    # Most capable general models first; the complete set remains selectable.
    models.sort(key=lambda item: ("flash" not in item.get("name", ""), "lite" in item.get("name", ""), item.get("name", "")))
    return [{"id":item["name"].removeprefix("models/"), "label":item.get("displayName") or item["name"].removeprefix("models/")} for item in models]

def _multipart_form(fields, file_field, filename, content, mime_type):
    """Build a small RFC 7578 body without adding a requests dependency."""
    boundary = "----JodSubBoundary" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(), b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {mime_type}\r\n\r\n".encode(),
        content, b"\r\n", f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), boundary

def groq_audio_captions(video, api_key, words_per_caption):
    """KarnSub-compatible transcription: Groq Whisper Large-v3 word timestamps."""
    api_key = str(api_key or "").strip().replace("\r", "").replace("\n", "")
    if not api_key: raise RuntimeError("ກະລຸນາໃສ່ Groq API key ສຳລັບ KarnSub Whisper Large‑V3.")
    if not api_key.strip().startswith("gsk_"):
        raise RuntimeError("key ນີ້ບໍ່ແມ່ນ Groq API key. Groq key ປົກກະຕິຕ້ອງຂຶ້ນຕົ້ນດ້ວຍ gsk_; Gemini key (AIza...) ໃຫ້ໃສ່ຊ່ອງ Gemini.")
    source = Path(video)
    wav = WORK / f"karnsub-{uuid.uuid4().hex}.wav"
    audio = WORK / f"karnsub-{uuid.uuid4().hex}.mp3"
    try:
        subprocess.run([ffmpeg(), "-y", "-i", str(source), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run([ffmpeg(), "-y", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "32k", str(audio)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if audio.stat().st_size > 25 * 1024 * 1024:
            raise RuntimeError("ສຽງໃຫຍ່ເກີນ 25 MB ສຳລັບ Groq; ຕັດວິດີໂອເປັນຊ່ວງກ່ອນ.")
        body, boundary = _multipart_form(
            {"model":"whisper-large-v3", "response_format":"verbose_json", "timestamp_granularities[]":"word", "language":"lo", "temperature":"0",
             "prompt":"ຖອດສຽງພາສາລາວເທົ່ານັ້ນ. ໃຊ້ອັກສອນລາວ, ຢ່າແປເປັນພາສາໄທ, ແລະຮັກສາຄຳເວົ້າທຸກຄຳ."},
            "file", audio.name, audio.read_bytes(), "audio/mpeg"
        )
        STATUS.update({"message":"ກຳລັງສົ່ງສຽງໄປ KarnSub Whisper Large‑V3 (40%)", "progress":40})
        request = urlrequest.Request("https://api.groq.com/openai/v1/audio/transcriptions", data=body, headers={"Authorization":f"Bearer {api_key}", "Content-Type":f"multipart/form-data; boundary={boundary}", "User-Agent":"JodSub/1.0 (Mac local app)", "Accept":"application/json"}, method="POST")
        try:
            with urlopen_retry(request, timeout=240) as response: decoded = json.loads(response.read().decode())
        except urlerror.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            if exc.code == 401:
                fingerprint = f"{api_key[:4]}… (ຄວາມຍາວ {len(api_key)})"
                raise RuntimeError(f"Groq HTTP 401: key {fingerprint} ບໍ່ຖືກຮັບຮອງ. key ອາດຖືກລົບ, ໝົດອາຍຸ, ຫຼືຄັດລອກບໍ່ຄົບ; ໃຫ້ສ້າງ Groq key ໃໝ່.") from exc
            if exc.code == 403 and "1010" in detail:
                raise RuntimeError("Groq HTTP 403 code 1010: Cloudflare ປະຕິເສດ User-Agent/ເຄືອຂ່າຍ; ລອງກົດອີກຄັ້ງ ຫຼືປ່ຽນເຄືອຂ່າຍ.") from exc
            if exc.code == 413:
                raise RuntimeError("Groq HTTP 413: ໄຟລ໌ສຽງໃຫຍ່ເກີນຂອບເຂດ; ລອງຫຼຸດຄຸນນະພາບ/ຕັດວິດີໂອເປັນຊ່ວງ.") from exc
            if exc.code == 429:
                raise RuntimeError("Groq HTTP 429: ໃຊ້ງານເກີນຈຳກັດຊົ່ວຄາວ; ລໍຖ້າແລ້ວກົດອີກຄັ້ງ.") from exc
            raise RuntimeError(f"Groq Whisper HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, urlerror.URLError) as exc:
            raise RuntimeError("Groq ເຊື່ອມຕໍ່ບໍ່ສຳເລັດ/ໝົດເວລາ; ກວດເນັດ ແລ້ວລອງອີກຄັ້ງ.") from exc
        raw_words = decoded.get("words", [])
        if not raw_words:
            # Some compatible responses return segments unless word granularity
            # is enabled; retain segment text as a safe diagnostic, not timing.
            raise RuntimeError("KarnSub Whisper Large‑V3 ບໍ່ສົ່ງ word timestamps ກັບມາ.")
        clean_words = []
        raw_script = []
        for item in raw_words:
            raw_text = str(item.get("word", item.get("text", ""))).strip()
            raw_script.append(raw_text)
            text = "".join(char for char in raw_text if "\u0e80" <= char <= "\u0eff")
            try: start, end = float(item["start"]), float(item["end"])
            except (KeyError, TypeError, ValueError): continue
            if not text or end <= start: continue
            tokens = lao_tokens(text)
            weight, cursor = max(1, sum(len(token) for token in tokens)), start
            for index, token in enumerate(tokens):
                token_end = end if index == len(tokens) - 1 else cursor + (end - start) * len(token) / weight
                clean_words.append({"start":round(cursor, 6), "end":round(token_end, 6), "text":token}); cursor = token_end
        if not clean_words:
            # Keep this diagnostic distinct from authentication/network errors:
            # Whisper can return Thai/Latin text for closely related speech
            # even when language=lo was requested.  The caller may then use
            # the configured Gemini Lao pass instead of saving bad subtitles.
            has_thai = any(any("\u0e00" <= char <= "\u0e7f" for char in text) for text in raw_script)
            detected = "ອັກສອນໄທ/ບໍ່ແມ່ນລາວ" if has_thai else "ຂໍ້ຄວາມບໍ່ແມ່ນອັກສອນລາວ"
            raise RuntimeError(f"KarnSub Whisper Large‑V3 ສົ່ງ{detected}; ບໍ່ໄດ້ບັນທຶກ subtitle ທີ່ຜິດພາສາ.")
        import soundfile as sf
        duration = sf.info(str(wav)).frames / sf.info(str(wav)).samplerate
        clean_words, scale = calibrate_word_clock(clean_words, duration)
        STATUS.update({"message":"ກຳລັງຈັບຂອບຄຳດ້ວຍ Lao CTC ຫຼັງ Whisper Large‑V3 (78%)", "progress":78})
        clean_words = force_align_lao_words(wav, clean_words, words_per_caption)
        captions = [{"start":group[0]["start"], "end":group[-1]["end"], "text":"".join(item["text"] for item in group), "words":copy.deepcopy(group)} for index in range(0, len(clean_words), words_per_caption) for group in [clean_words[index:index + words_per_caption]]]
        captions = correct_caption_phase(wav, captions)
        captions = snap_caption_edges(wav, captions)
        return format_captions(captions, words_per_caption)
    finally:
        audio.unlink(missing_ok=True); wav.unlink(missing_ok=True)

def audio_has_activity_after(wav_path, start, duration):
    """Distinguish a genuinely missing tail from ordinary trailing silence."""
    try:
        import numpy as np, soundfile as sf
        samples, rate = sf.read(str(wav_path), always_2d=False, dtype="float32")
        if getattr(samples, "ndim", 1) > 1: samples = samples.mean(axis=1)
        samples = np.asarray(samples, dtype=float)
        tail = samples[max(0, int(start * rate)):min(len(samples), int(duration * rate))]
        if len(tail) < max(1, int(rate * .15)): return False
        step = max(1, int(rate * .02)); usable = len(tail) - len(tail) % step
        if usable < step * 4: return False
        rms = np.sqrt(np.mean(tail[:usable].reshape(-1, step) ** 2, axis=1))
        floor, peak = float(np.percentile(rms, 18)), float(np.percentile(rms, 99))
        threshold = max(.0025, floor * 2.4, floor + (peak - floor) * .075)
        return bool(np.count_nonzero(rms >= threshold) >= max(3, len(rms) // 20))
    except Exception:
        return True

def gemini_tail_words(wav_path, api_key, model, offset, duration):
    """Ask Gemini for a missing tail using timestamps relative to that tail."""
    tail_wav = WORK / f"jodsub-tail-{uuid.uuid4().hex}.wav"
    tail_audio = WORK / f"jodsub-tail-{uuid.uuid4().hex}.mp3"
    try:
        subprocess.run([ffmpeg(), "-y", "-ss", f"{offset:.6f}", "-i", str(wav_path), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(tail_wav)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run([ffmpeg(), "-y", "-i", str(tail_wav), "-c:a", "libmp3lame", "-b:a", "24k", str(tail_audio)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        prompt = ("Transcribe every spoken Lao word in this audio tail. Return ONLY valid JSON with this exact shape: "
                  '{"words":[{"start":0.000,"end":0.000,"text":"ລາວ"}]}. '
                  "Timestamps are relative to the beginning of this tail. Include every word through the end; do not stop early; Lao script only; do not add spaces.")
        payload = {"contents":[{"parts":[{"text":prompt},{"inlineData":{"mimeType":"audio/mpeg","data":base64.b64encode(tail_audio.read_bytes()).decode("ascii")}}]}], "generationConfig":{"responseMimeType":"application/json", "temperature":0}}
        request = urlrequest.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json", "x-goog-api-key":api_key}, method="POST")
        with urlopen_retry(request, timeout=240) as response: body = json.loads(response.read().decode())
        raw = body["candidates"][0]["content"]["parts"][0]["text"]
        decoded = json.loads(re.search(r"\{[\s\S]*\}", raw).group(0))
        result = []
        for word in decoded.get("words", []):
            text = "".join(char for char in str(word.get("text", "")) if "\u0e80" <= char <= "\u0eff")
            start, end = float(word["start"]) + offset, float(word["end"]) + offset
            if text and end > start: result.append({"start":start, "end":end, "text":text})
        return result
    finally:
        tail_wav.unlink(missing_ok=True); tail_audio.unlink(missing_ok=True)

def _seconds_offset(value):
    """Parse Gemini interaction offsets such as '1.250s' without guessing."""
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)s\s*", str(value or ""))
    return float(match.group(1)) if match else None

def gemini_upload_file(path, api_key, mime_type="audio/mp3"):
    size = path.stat().st_size
    start = urlrequest.Request(
        "https://generativelanguage.googleapis.com/upload/v1beta/files",
        data=json.dumps({"file":{"display_name":path.name}}).encode(),
        headers={
            "x-goog-api-key": api_key,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
        },
        method="POST")
    try:
        with urlopen_retry(start, timeout=60) as response:
            upload_url = response.headers.get("x-goog-upload-url")
    except urlerror.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:320]
        raise RuntimeError(f"Gemini Files upload start HTTP {exc.code}: {detail}") from exc
    if not upload_url:
        raise RuntimeError("Gemini Files API ບໍ່ໄດ້ສົ່ງ upload URL.")
    upload = urlrequest.Request(
        upload_url,
        data=path.read_bytes(),
        headers={
            "Content-Length": str(size),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        method="POST")
    try:
        with urlopen_retry(upload, timeout=180) as response:
            result = json.loads(response.read().decode())
    except urlerror.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:320]
        raise RuntimeError(f"Gemini Files upload HTTP {exc.code}: {detail}") from exc
    file_info = result.get("file", result)
    uri = file_info.get("uri")
    if not uri:
        raise RuntimeError("Gemini Files API ອັບໂຫຼດສຽງແລ້ວແຕ່ບໍ່ມີ file URI.")
    return {"uri": uri, "mime_type": file_info.get("mimeType") or mime_type, "name": file_info.get("name", "")}

def gemini_delete_file(name, api_key):
    if not name:
        return
    request = urlrequest.Request(
        f"https://generativelanguage.googleapis.com/v1beta/{name}",
        headers={"x-goog-api-key": api_key},
        method="DELETE")
    try:
        urlopen_retry(request, timeout=30).close()
    except Exception:
        pass

def gemini_native_word_times(audio, api_key):
    """Use Gemini's transcription endpoint, not prompted timestamp JSON.

    This is deliberately a single Gemini speech model.  It returns native
    word_info annotations, so the app never invents an approximate word clock
    from character count or asks a general-purpose model to make timestamps.
    """
    file_info = gemini_upload_file(audio, api_key, "audio/mp3")
    try:
        payload = {
            "model": "gemini-3.5-transcribe",
            "input": [{"type":"audio", "uri":file_info["uri"], "mime_type":file_info["mime_type"]}],
            "generation_config": {"transcription_config": {"mode": {"type":"verbatim", "timestamp_granularities":["word"]}}},
        }
        request = urlrequest.Request(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            data=json.dumps(payload).encode(), headers={"Content-Type":"application/json", "x-goog-api-key":api_key}, method="POST")
        try:
            with urlopen_retry(request, timeout=240) as response: interaction = json.loads(response.read().decode())
        except urlerror.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:320]
            raise RuntimeError(f"Gemini 3.5 Transcribe HTTP {exc.code}: {detail}") from exc
        except (TimeoutError, urlerror.URLError) as exc:
            raise RuntimeError("Gemini 3.5 Transcribe ໝົດເວລາ/ຕິດຂັດເຄືອຂ່າຍ.") from exc
        words = []
        for step in interaction.get("steps", []) or []:
            for content in step.get("content", []) or []:
                for item in content.get("annotations", []) or []:
                    if item.get("type") != "word_info": continue
                    start, end = _seconds_offset(item.get("start_offset")), _seconds_offset(item.get("end_offset"))
                    text = "".join(char for char in str(item.get("text", "")) if "\u0e80" <= char <= "\u0eff")
                    if text and start is not None and end is not None and end > start:
                        # Keep native annotation units intact.  Lao segmentation
                        # happens later only for display rows, not timing.
                        words.append({"start":round(start, 6), "end":round(end, 6), "text":text})
        if not words:
            raise RuntimeError("Gemini 3.5 Transcribe ບໍ່ໄດ້ສົ່ງ word timestamps ພາສາລາວ; ບໍ່ໄດ້ສ້າງ subtitle ທີ່ຄາດເດົາ.")
        return words
    finally:
        gemini_delete_file(file_info.get("name", ""), api_key)

def gemini_prompted_word_times(audio, api_key, selected_model):
    """Fallback for Gemini models that return text but not native word_info."""
    models = gemini_models(api_key)
    ids = [str(item.get("id", "")) for item in models if item.get("id")]
    if selected_model and selected_model != "auto": ids = [selected_model]
    if not ids: raise RuntimeError("API key ນີ້ບໍ່ມີ Gemini model ທີ່ໃຊ້ໄດ້.")
    prompt = ('Transcribe every spoken Lao word. Return ONLY JSON with this exact shape: '
              '{"words":[{"start":0.000,"end":0.000,"text":"ລາວ"}]}. '
              'Use Lao script only, include every spoken word in order, and use precise seconds.')
    payload = {"contents":[{"parts":[{"text":prompt},{"inlineData":{"mimeType":"audio/mpeg","data":base64.b64encode(Path(audio).read_bytes()).decode("ascii")}}]}], "generationConfig":{"responseMimeType":"application/json","temperature":0}}
    failures = []
    for model in ids:
        try:
            request = urlrequest.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json", "x-goog-api-key":api_key}, method="POST")
            with urlopen_retry(request, timeout=240) as response: body = json.loads(response.read().decode())
            raw = body["candidates"][0]["content"]["parts"][0]["text"]
            decoded = json.loads(re.search(r"\{[\s\S]*\}", raw).group(0))
            words = []
            for item in decoded.get("words", []):
                text = "".join(char for char in str(item.get("text", "")) if "\u0e80" <= char <= "\u0eff")
                start, end = float(item.get("start", 0)), float(item.get("end", 0))
                if text and end > start: words.append({"start":start, "end":end, "text":text})
            if words: return words
            failures.append(f"{model}: ບໍ່ມີຄຳ")
        except Exception as exc:
            failures.append(f"{model}: {str(exc)[:100]}")
    raise RuntimeError("Gemini fallback ບໍ່ສຳເລັດ: " + " | ".join(failures[-2:]))

def gemini_audio_captions(video, api_key, words_per_caption, selected_model):
    """Direct JodSub -> Gemini native transcription with validated word timing."""
    if not api_key: raise RuntimeError("ກະລຸນາໃສ່ Gemini API key.")
    source = Path(video)
    token = uuid.uuid4().hex
    audio, wav = WORK / f"jodsub-{token}.mp3", WORK / f"jodsub-{token}.wav"
    try:
        STATUS.update({"message":"ກຳລັງແປງວິດີໂອເປັນສຽງສຳລັບ Gemini (20%)", "progress":20})
        # WAV is retained only during this job to refine caption edges locally.
        subprocess.run([ffmpeg(), "-y", "-i", str(source), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run([ffmpeg(), "-y", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "24k", str(audio)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if audio.stat().st_size > 18 * 1024 * 1024:
            raise RuntimeError("ສຽງຍາວເກີນ 18 MB ສຳລັບການສົ່ງກົງ. ຕັດວິດີໂອເປັນສ່ວນກ່ອນ.")
        STATUS.update({"message":"ກຳລັງຖອດສຽງດ້ວຍ Gemini 3.5 Transcribe ແບບ word-level (55%)", "progress":55})
        native_timing = True
        try:
            clean_words = gemini_native_word_times(audio, api_key)
        except RuntimeError:
            native_timing = False
            STATUS.update({"message":"Gemini ບໍ່ສົ່ງ word timestamps — ກຳລັງໃຊ້ໂໝດສຳຮອງ (65%)", "progress":65})
            clean_words = gemini_prompted_word_times(audio, api_key, selected_model)
        if not clean_words:
            raise RuntimeError("Gemini ບໍ່ສາມາດສົ່ງ timestamp ພາສາລາວໄດ້.")
        import soundfile as sf
        wav_info = sf.info(str(wav)); audio_duration = wav_info.frames / wav_info.samplerate
        clean_words, clock_scale = calibrate_word_clock(clean_words, audio_duration)
        # Gemini's prompted fallback can emit fluent text with a compressed,
        # nearly continuous clock.  Phrase-local Lao CTC follows the known
        # transcript without changing a single word and accepts a correction
        # only when the recording provides sufficient lexical evidence.  This
        # is intentionally not the removed whole-video ASR/remap pass that
        # caused cumulative drift in older builds.
        if (LAO_AUDITOR_DIR / "model.safetensors").is_file():
            STATUS.update({"message":"ກຳລັງກວດ word timestamps ກັບສຽງລາວຕົ້ນສະບັບ (76%)", "progress":76})
            whisperx_words = whisperx_force_align_lao_words(wav, clean_words)
            clean_words = whisperx_words or force_align_lao_words(wav, clean_words, words_per_caption)
        elif not native_timing:
            raise RuntimeError("Gemini fallback ບໍ່ມີ native word timestamps ແລະບໍ່ພົບ Lao alignment model; ບໍ່ໄດ້ບັນທຶກເວລາທີ່ຄາດເດົາ.")
        for index in range(1, len(clean_words)):
            if clean_words[index]["start"] < clean_words[index - 1]["end"]:
                clean_words[index]["start"] = clean_words[index - 1]["end"]
                clean_words[index]["end"] = max(clean_words[index]["start"] + .012, clean_words[index]["end"])
        captions = [{"start":group[0]["start"], "end":group[-1]["end"], "text":"".join(item["text"] for item in group), "words":copy.deepcopy(group)} for index in range(0, len(clean_words), max(2, min(5, int(words_per_caption)))) for group in [clean_words[index:index + max(2, min(5, int(words_per_caption)))]]]
        STATUS.update({"message":"ກຳລັງຈັບຂອບ subtitle ຈາກ word timestamps ຈິງ (88%)", "progress":88})
        captions = snap_caption_edges(wav, captions)
        return format_captions(captions, words_per_caption)
        # Legacy prompted-JSON code is intentionally unreachable.  It is
        # retained below only for source compatibility while the app updates.
        models = gemini_models(api_key)
        model_ids = [item["id"] for item in models]
        if selected_model and selected_model != "auto":
            if selected_model not in model_ids: raise RuntimeError("model ທີ່ເລືອກບໍ່ໄດ້ຮອງຮັບສຳລັບ API key ນີ້.")
            model_ids = [selected_model]
        if not model_ids: raise RuntimeError("API key ນີ້ບໍ່ມີ Gemini model ທີ່ໃຊ້ generateContent ໄດ້.")
        prompt = ("Transcribe every spoken Lao word in this audio. Return ONLY valid JSON with this exact shape: "
                  '{"words":[{"start":0.000,"end":0.000,"text":"ລາວ"}]}. '
                  "Use Lao script only; do not translate; include all spoken words in order; do not add spaces inside Lao text; use precise seconds.")
        payload = {"contents":[{"parts":[{"text":prompt},{"inlineData":{"mimeType":"audio/mpeg","data":base64.b64encode(audio.read_bytes()).decode("ascii")}}]}], "generationConfig":{"responseMimeType":"application/json","temperature":0}}
        failures = []
        for number, model in enumerate(model_ids, 1):
            progress = min(90, 35 + int(number * 55 / max(1, len(model_ids))))
            STATUS.update({"message":f"ກຳລັງສົ່ງສຽງໄປ Gemini: {model} ({progress}%)", "progress":progress})
            request = urlrequest.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json", "x-goog-api-key":api_key}, method="POST")
            try:
                with urlopen_retry(request, timeout=240) as response: body = json.loads(response.read().decode())
                raw = body["candidates"][0]["content"]["parts"][0]["text"]
                decoded = json.loads(re.search(r"\{[\s\S]*\}", raw).group(0))
                words = decoded.get("words", [])
                if not words: raise RuntimeError("Gemini ບໍ່ພົບຄຳເວົ້າໃນສຽງ.")
                clean_words = []
                for word in words:
                    text = "".join(char for char in str(word.get("text", "")) if "\u0e80" <= char <= "\u0eff")
                    start, end = float(word["start"]), float(word["end"])
                    tokens = lao_tokens(text)
                    if tokens and end > start:
                        weight, cursor = max(1, sum(len(token) for token in tokens)), start
                        for index, token in enumerate(tokens):
                            token_end = end if index == len(tokens) - 1 else cursor + (end - start) * len(token) / weight
                            clean_words.append({"start":cursor, "end":token_end, "text":token}); cursor = token_end
                if not clean_words: raise RuntimeError("Gemini ສົ່ງຂໍ້ຄວາມທີ່ບໍ່ແມ່ນພາສາລາວ.")
                import soundfile as sf
                wav_info = sf.info(str(wav)); audio_duration = wav_info.frames / wav_info.samplerate
                reported_end = max(float(item["end"]) for item in clean_words)
                # Gemini occasionally stops returning JSON at an earlier
                # utterance even though the audio continues.  Probe that
                # missing tail before accepting the result; this prevents a
                # silent 35-second subtitle from being saved for a 46-second
                # video.  Ordinary trailing silence is accepted.
                if reported_end < audio_duration - .75 and audio_has_activity_after(wav, reported_end, audio_duration):
                    STATUS.update({"message":"Gemini ສົ່ງຄຳບໍ່ຄົບ — ກຳລັງຖອດຊ່ວງທ້າຍຊ້ຳ (84%)", "progress":84})
                    tail = gemini_tail_words(wav, api_key, model, max(0.0, reported_end - .8), audio_duration)
                    if not tail or max(float(item["end"]) for item in tail) <= reported_end + .20:
                        raise RuntimeError(f"Gemini ຖອດສຽງບໍ່ຄົບ: ພົບສຽງຫຼັງ {reported_end:.2f}s ແຕ່ບໍ່ໄດ້ຄຳທ້າຍກັບມາ.")
                    # Remove overlap by keeping the first occurrence of the
                    # tail words; preserve Gemini's original earlier text.
                    overlap = max((index for index, item in enumerate(clean_words) if any(item["text"] == tail_item["text"] and abs(float(item["start"]) - float(tail_item["start"])) < 1.0 for tail_item in tail)), default=-1)
                    prefix = clean_words[:overlap + 1] if overlap >= 0 else [item for item in clean_words if float(item["end"]) <= reported_end - .35]
                    clean_words = prefix + [item for item in tail if float(item["start"]) > reported_end - .35]
                clean_words, clock_scale = calibrate_word_clock(clean_words, audio_duration)
                if clock_scale != 1.0:
                    STATUS.update({"message":f"ກຳລັງແກ້ clock subtitle ໃຫ້ກົງກັບຄວາມຍາວສຽງ ({clock_scale * 100:.2f}%)", "progress":84})
                # Gemini supplies the visible words.  Once the OpenAI
                # checkpoint download is complete, Whisper Large-v3 adds an
                # independent word-timestamp pass before the Lao CTC guard.
                if whisperx_v3_ready() or openai_whisper_v3_ready():
                    precision_name = "WhisperX word-level + Lao CTC" if whisperx_v3_ready() else "OpenAI Whisper Large‑V3 + Lao CTC"
                    STATUS.update({"message":f"ກຳລັງກວດຕຳແໜ່ງຄຳດ້ວຍ {precision_name} (88%)", "progress":88})
                    clean_words = refine_gemini_words_with_whisper_v3(wav, clean_words)
                # The language-specific Lao CTC model then aligns known words
                # to local acoustic frames and rejects low-confidence phrases.
                STATUS.update({"message":"ກຳລັງກວດຕຳແໜ່ງແຕ່ລະຄຳດ້ວຍ Lao acoustic aligner (91%)", "progress":91})
                clean_words = force_align_lao_words(wav, clean_words, words_per_caption)
                captions = [{"start":group[0]["start"], "end":group[-1]["end"], "text":"".join(item["text"] for item in group), "words":copy.deepcopy(group)} for index in range(0, len(clean_words), words_per_caption) for group in [clean_words[index:index + words_per_caption]]]
                STATUS.update({"message":"ກຳລັງຈັບຂອບ subtitle ຈາກສຽງຈິງ (94%)", "progress":94})
                captions = correct_caption_phase(wav, captions)
                captions = snap_caption_edges(wav, captions)
                return format_captions(captions, words_per_caption)
            except Exception as exc:
                failures.append(f"{model}: {str(exc)[:120]}")
                if selected_model and selected_model != "auto": raise RuntimeError(f"Gemini {model} ບໍ່ສຳເລັດ: {exc}") from exc
        raise RuntimeError("Gemini ບໍ່ສາມາດຖອດສຽງໄດ້ທຸກ model. " + " | ".join(failures[-3:]))
    finally:
        audio.unlink(missing_ok=True); wav.unlink(missing_ok=True)

def gemini_text(prompt, api_key, model):
    if not api_key: raise RuntimeError("ກະລຸນາໃສ່ Gemini API key.")
    target = model if model and model != "auto" else "gemini-2.5-flash-lite"
    request = urlrequest.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{target}:generateContent?key={api_key}",
        data=json.dumps({"contents":[{"parts":[{"text":prompt}]}], "generationConfig":{"temperature":0.1}}).encode(),
        headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urlopen_retry(request, timeout=90) as response:
            data = json.loads(response.read().decode())
    except urlerror.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"Gemini ເຊື່ອມຕໍ່ບໍ່ສຳເລັດ: {exc}") from exc
    try: return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc: raise RuntimeError("Gemini ບໍ່ສົ່ງຂໍ້ຄວາມກັບມາ.") from exc

def status_pulse(message, start=12, ceiling=94):
    """Keep the UI alive during model/network calls which expose no native progress."""
    stop = threading.Event()
    def tick():
        progress = start
        increment = max(1, (ceiling - start) // 32)
        while not stop.wait(2):
            if STATUS.get("state") != "working": return
            progress = min(ceiling, progress + increment)
            STATUS.update({"message": f"{message} ({progress}%)", "progress": progress})
    threading.Thread(target=tick, daemon=True).start()
    return stop

def ai_job(project_id, action, payload):
    global STATUS
    pulse = None
    try:
        project = load_project(project_id)
        names = {"transcribe":"ກຳລັງຖອດສຽງ ແລະຈັບເວລາ subtitle", "diarize":"ກຳລັງຖອດສຽງແຍກຄົນເວົ້າ", "realign":"ກຳລັງຈັບເວລາ subtitle ຄືນໃໝ່", "translate":"ກຳລັງແປ subtitle", "format":"ກຳລັງຈັດ subtitle ເປັນບັນທັດ", "sfx":"ກຳລັງວິເຄາະ subtitle ເພື່ອຈັດ SFX"}
        activity = names.get(action, "ກຳລັງປະມວນຜົນ")
        STATUS = {"state":"working", "message":f"{activity} (10%)", "progress":10, "files":[]}
        pulse = status_pulse(activity)
        if action == "transcribe":
            STATUS.update({"message":"ກຳລັງກຽມສຽງຈາກວິດີໂອ (15%)", "progress":15})
            # The primary Transcribe button is a Gemini workflow.  Require an
            # explicit `karnsub` engine before treating any key as a Groq key;
            # older clients omitted the field and were accidentally routed to
            # Groq even though their visible field and key were Gemini.
            engine = str(payload.get("engine") or "gemini").strip().lower()
            if engine == "karnsub":
                # Older UI builds call the field apiKey.  Accept it as the
                # Groq key so an update does not silently route to Gemini.
                try:
                    captions = groq_audio_captions(project["files"]["video"], payload.get("groqApiKey", "") or payload.get("apiKey", ""), int(payload.get("words", 3)))
                except RuntimeError as exc:
                    # Groq is the primary KarnSub pass.  If Whisper returns
                    # no Lao-script words (usually Thai/Latin for mixed
                    # speech), use the user's Gemini Lao key as a controlled
                    # language fallback instead of writing an empty/bad SRT.
                    if "ສົ່ງອັກສອນ" not in str(exc) or not str(payload.get("geminiApiKey", "")).strip():
                        raise
                    STATUS.update({"message":"KarnSub ສົ່ງອັກສອນບໍ່ແມ່ນລາວ — ກຳລັງໃຊ້ Gemini Lao fallback (55%)", "progress":55})
                    captions = gemini_audio_captions(project["files"]["video"], payload.get("geminiApiKey", "").strip(), int(payload.get("words", 3)), payload.get("model", "auto"))
            elif engine == "gemini":
                captions = gemini_audio_captions(project["files"]["video"], payload.get("apiKey", ""), int(payload.get("words", 3)), payload.get("model", "auto"))
            else:
                raise RuntimeError(f"ບໍ່ຮູ້ຈັກ transcription engine: {engine}")
            # Both engines already return provider word timestamps and apply
            # their own bounded edge refinement.  Do not run the manual
            # full-caption realigner here: it redistributes words from audio
            # amplitude and previously moved correct Gemini word_info timing.
            # Manual realignment remains available as an explicit user action.
            if project.get("captions"):
                history = project.setdefault("caption_history", [])
                history.append({
                    "saved_at": time.time(),
                    "reason": "before_transcribe",
                    "captions": copy.deepcopy(project["captions"]),
                    "transcription": copy.deepcopy(project.get("transcription", {})),
                })
                project["caption_history"] = history[-3:]
            STATUS.update({"message":"ກຳລັງບັນທຶກ subtitle (96%)", "progress":96})
            project["captions"] = [{"start":float(c["start"]), "end":float(c["end"]), "text":str(c["text"]), "words":c.get("words", [])} for c in captions]
            video_path = Path(project["files"]["video"])
            project["transcription"] = {
                "engine": engine,
                "timing_source": "whisperx_lao_ctc_forced_alignment" if (LAO_AUDITOR_DIR / "model.safetensors").is_file() else "provider_word_timestamps",
                "source_name": video_path.name,
                "source_size": video_path.stat().st_size if video_path.is_file() else None,
                "source_duration_seconds": media_duration(video_path) / 1000000.0 if video_path.is_file() else 0.0,
                "created_at": time.time(),
            }
            srt = EXPORTS / f"{clean(project['name'])}.srt"
            write_srt(project["captions"], srt)
            project["srt_file"] = srt.name
            message = f"ຖອດສຽງສຳເລັດ {len(project['captions'])} subtitle."
        elif action == "diarize":
            STATUS.update({"message":"ກຳລັງກຽມສຽງແລະໂມເດວແຍກຄົນເວົ້າ (15%)", "progress":15})
            captions = whisperx_diarized_captions(project["files"]["video"], payload.get("hfToken", ""), int(payload.get("words", 3)), bool(payload.get("fast", False)))
            project["captions"] = [{"start":float(c["start"]), "end":float(c["end"]), "text":str(c["text"]), "speaker":str(c.get("speaker", "")), "words":c.get("words", [])} for c in captions]
            srt = EXPORTS / f"{clean(project['name'])}-speakers.srt"; write_srt(project["captions"], srt); project["srt_file"] = srt.name
            message = f"ຖອດສຽງແຍກຄົນເວົ້າສຳເລັດ {len(project['captions'])} subtitle."
        elif action == "assembly-diarize":
            STATUS.update({"message":"ກຳລັງສົ່ງສຽງໄປ AssemblyAI ເພື່ອແຍກຄົນເວົ້າ (15%)", "progress":15})
            speaker_captions = assemblyai_diarized_captions(project["files"]["video"], payload.get("apiKey", ""), int(payload.get("words", 3)))
            gemini_key = str(payload.get("geminiApiKey", "")).strip()
            if not gemini_key:
                raise RuntimeError("ໃສ່ Gemini API key ນຳ ເພື່ອໃຫ້ subtitle ເປັນພາສາລາວ.")
            STATUS.update({"message":"ກຳລັງໃຊ້ Gemini ຖອດຂໍ້ຄວາມລາວ ແລະນຳ speaker timestamps ມາຈັບຄູ່ (75%)", "progress":75})
            text_captions = gemini_audio_captions(project["files"]["video"], gemini_key, int(payload.get("words", 3)), payload.get("model", "auto"))
            project["captions"] = []
            for caption in text_captions:
                overlaps = []
                for spoken in speaker_captions:
                    overlap = max(0.0, min(float(caption["end"]), float(spoken["end"])) - max(float(caption["start"]), float(spoken["start"])))
                    if overlap: overlaps.append((overlap, spoken.get("speaker", "ຄົນເວົ້າ A")))
                speaker = max(overlaps, default=(0, "ຄົນເວົ້າ A"))[1]
                item = dict(caption); item["speaker"] = speaker; project["captions"].append(item)
            srt = EXPORTS / f"{clean(project['name'])}-speakers-cloud.srt"; write_srt(project["captions"], srt); project["srt_file"] = srt.name
            message = f"AssemblyAI ແຍກຄົນເວົ້າສຳເລັດ {len(project['captions'])} subtitle."
        elif action == "realign":
            history = project.setdefault("caption_history", [])
            history.append({
                "saved_at": time.time(),
                "reason": "before_realign",
                "captions": copy.deepcopy(project.get("captions", [])),
                "transcription": copy.deepcopy(project.get("transcription", {})),
            })
            project["caption_history"] = history[-3:]
            captions, scale = local_realign_captions(project["files"]["video"], project.get("captions", []), int(payload.get("words", project.get("config", {}).get("wordsPerCaption", 3))))
            project["captions"] = captions
            project.setdefault("transcription", {})["timing_source"] = "whisperx_lao_ctc_forced_alignment"
            project["transcription"]["realigned_at"] = time.time()
            requested_sfx = max(0, min(20, int(project.get("config", {}).get("sfxCount", 0))))
            if requested_sfx:
                # Existing events may carry the pre-calibration clock.  Build
                # them from the corrected captions instead of reusing stale
                # positions outside the video.
                project["sfx_events"] = sfx_events(captions, requested_sfx, project_id)
            srt = EXPORTS / f"{clean(project['name'])}.srt"; write_srt(project["captions"], srt); project["srt_file"] = srt.name
            clock_note = f" ແກ້ clock ເປັນ {scale * 100:.2f}% ຕາມຄວາມຍາວສຽງ." if scale != 1.0 else ""
            message = f"ຈັບເວລາ subtitle ຄືນໃໝ່ສຳເລັດ {len(captions)} ບັນທັດ.{clock_note}"
        elif action == "translate":
            captions = project.get("captions", [])
            if not captions: raise RuntimeError("ຍັງບໍ່ມີ subtitle ໃຫ້ແປ.")
            source = [c["text"] for c in captions]
            prompt = "Translate every subtitle into " + payload.get("language", "English") + ". Keep the same count and return only a JSON array of strings. Do not add commentary.\n" + json.dumps(source, ensure_ascii=False)
            result = gemini_text(prompt, payload.get("apiKey", ""), payload.get("model", ""))
            match = re.search(r"\[[\s\S]*\]", result)
            translated = json.loads(match.group(0) if match else result)
            if not isinstance(translated, list) or len(translated) != len(captions): raise RuntimeError("Gemini ສົ່ງຈຳນວນ subtitle ບໍ່ຄົບ; ບໍ່ໄດ້ຂຽນທັບຂໍ້ຄວາມເກົ່າ.")
            for caption, text in zip(captions, translated):
                caption.setdefault("original_text", caption["text"]); caption["text"] = str(text).strip()
            message = "ແປ subtitle ສຳເລັດ. ຂໍ້ຄວາມກ່ອນແປຍັງຢູ່ໃນ project."
        elif action == "format":
            project["captions"] = format_captions(project.get("captions", []), int(payload.get("words", 3)))
            if not project["captions"]: raise RuntimeError("ບໍ່ມີ subtitle ສຳລັບຈັດບັນທັດ.")
            srt = EXPORTS / f"{clean(project['name'])}.srt"; write_srt(project["captions"], srt); project["srt_file"] = srt.name
            message = f"ຈັດ subtitle ສຳເລັດ: ບໍ່ເກີນ {int(payload.get('words', 3))} ຄຳຕໍ່ບັນທັດ."
        elif action == "sfx":
            project["sfx_events"] = sfx_events(project.get("captions", []), payload.get("count", 0), project_id)
            message = f"ຈັດ SFX ອັດຕະໂນມັດ {len(project['sfx_events'])} ສຽງແລ້ວ."
        else: raise RuntimeError("ຄຳສັ່ງ AI ບໍ່ຖືກຮອງຮັບ.")
        project["updated"] = time.time(); save_project(project)
        STATUS = {"state":"done", "message":message, "progress":100, "files":[project["srt_file"]] if action in ("transcribe", "diarize", "realign", "format") else []}
    except Exception as exc:
        STATUS = {"state":"error", "message":f"JodSub ບໍ່ສຳເລັດ: {str(exc)[:600]}", "progress":0, "files":[]}
    finally:
        if pulse: pulse.set()

def capcut_drafts():
    """Yield real editable drafts, excluding JodSub output and the recycle bin."""
    if not CAPCUT_ROOT.is_dir(): return
    for folder in CAPCUT_ROOT.iterdir():
        if not folder.is_dir() or folder.name.startswith(("JodSub-", ".")): continue
        for draft in sorted(folder.glob("Timelines/*/draft_info.json")):
            yield folder, draft

def select_capcut_source_draft(drafts):
    """Choose the least-modified timeline that still contains real video."""
    candidates = []
    for draft in drafts:
        try:
            data = json.loads(draft.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        tracks = data.get("tracks", [])
        video_segments = sum(
            len(track.get("segments", []))
            for track in tracks if track.get("type") == "video"
        )
        if not video_segments:
            continue
        generated = sum(1 for track in tracks if str(track.get("name", "")).startswith("JodSub"))
        non_video = sum(1 for track in tracks if track.get("type") != "video")
        candidates.append(((generated, non_video, len(tracks), draft.parent.name), draft))
    return min(candidates, key=lambda item:item[0])[1] if candidates else None

def capcut_projects():
    """Show one picker item per CapCut project; timelines stay internal."""
    projects = []
    seen = set()
    for folder, draft in capcut_drafts() or []:
        if folder.name in seen: continue
        seen.add(folder.name)
        candidates = [item for item_folder, item in capcut_drafts() or [] if item_folder == folder]
        chosen = select_capcut_source_draft(candidates)
        if not chosen:
            continue
        timeline_id = chosen.parent.name
        projects.append({"id":f"{folder.name}::{timeline_id}", "name":folder.name, "timeline":str(chosen)})
    return sorted(projects, key=lambda item: item["name"].casefold())

def capcut_draft_from_id(capcut_id):
    """Resolve a UI draft id safely without accepting a filesystem path."""
    value = str(capcut_id)
    folder_name, separator, timeline_id = value.partition("::")
    if not folder_name or any(part in folder_name for part in ("/", "\\", "..")):
        raise RuntimeError("CapCut project ທີ່ເລືອກບໍ່ຖືກຕ້ອງ.")
    folder = CAPCUT_ROOT / folder_name
    if not folder.is_dir() or folder.name.startswith(("JodSub-", ".")):
        raise RuntimeError("ບໍ່ພົບ CapCut project ທີ່ເລືອກ.")
    drafts = sorted(folder.glob("Timelines/*/draft_info.json"))
    if separator:
        if not re.fullmatch(r"[A-Za-z0-9-]+", timeline_id): raise RuntimeError("CapCut timeline ທີ່ເລືອກບໍ່ຖືກຕ້ອງ.")
        requested = next((draft for draft in drafts if draft.parent.name == timeline_id), None)
        if requested:
            try:
                requested_data = json.loads(requested.read_text(encoding="utf-8"))
                if any(track.get("type") == "video" and track.get("segments") for track in requested_data.get("tracks", [])):
                    return folder, requested
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
    chosen = select_capcut_source_draft(drafts)
    if not chosen: raise RuntimeError("ບໍ່ພົບ timeline ທີ່ມີ video ໃນ CapCut project ທີ່ເລືອກ.")
    return folder, chosen

def editing_style_media_roots():
    """Known local media locations used only to resolve stale CapCut paths."""
    roots = [ROOT / " videon oljiner", ROOT / "work"]
    return [folder for folder in roots if folder.is_dir()]

def write_editing_style_report(report):
    """Persist a machine-readable report and a concise human-readable summary."""
    name = f"editing-style-{int(report['created_at'])}-{report['id'][:8]}"
    report_path = EXPORTS / f"{name}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    silence = report.get("silence_observations", {})
    audio = report.get("audio_measurement", {})
    lines = [
        "# JodSub Editing Style Analysis",
        "", f"Source video: {report.get('source_video', '')}",
        f"Original duration: {report.get('source_duration_s')} s",
        f"Edited kept duration: {report.get('edited_duration_s')} s",
        f"Removed intervals found: {len(report.get('removed_segments', []))}",
        f"Measured noise floor: {audio.get('noise_floor_db')} dB",
        f"Measured candidate activity threshold: {audio.get('candidate_activity_threshold_db')} dB",
        f"Quiet-cut evidence: {silence.get('count')}",
        f"Quiet-cut duration min/avg/max: {silence.get('minimum_ms')} / {silence.get('average_ms')} / {silence.get('maximum_ms')} ms",
        f"Measured pre/post roll: {silence.get('pre_roll_ms')} / {silence.get('post_roll_ms')} ms",
        "", "## Removed intervals",
    ]
    for item in report.get("removed_segments", []):
        lines.append(f"- {item['start']:.3f}–{item['end']:.3f}s | {item['duration_ms']} ms | RMS {item['rms_db']} dB | peak {item['peak_db']} dB | {item['classification']}")
    if report.get("limitations"):
        lines += ["", "## Limitations"] + [f"- {item}" for item in report["limitations"]]
    markdown_path = EXPORTS / f"{name}.md"
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path, markdown_path

def analyze_editing_style_job(capcut_id, original_srt=None, edited_srt=None):
    """Analyze one reference asynchronously; source media is never modified."""
    global STATUS
    try:
        STATUS = {"state":"working", "message":"ກຳລັງວິເຄາະການຕັດ ແລະ waveform ຈາກ reference project…", "progress":20, "files":[]}
        _, draft = capcut_draft_from_id(capcut_id)
        report = analyze_capcut_reference(
            draft, ffmpeg(), original_srt=original_srt, edited_srt=edited_srt,
            media_roots=editing_style_media_roots(),
        )
        STATUS.update({"message":"ກຳລັງສ້າງ Editing Profile ຈາກຂໍ້ມູນທີ່ວັດໄດ້…", "progress":82})
        profile = update_editing_profile(load_editing_profile(EDITING_STYLE_PROFILE), report)
        save_editing_profile(EDITING_STYLE_PROFILE, profile)
        report_path, markdown_path = write_editing_style_report(report)
        STATUS = {"state":"done", "message":f"ວິເຄາະ reference ສຳເລັດ: ພົບ {len(report['removed_segments'])} ຊ່ວງທີ່ຖືກຕັດ; ກົດອ່ານ report ໄດ້.", "progress":100, "files":[report_path.name, markdown_path.name]}
    except Exception as exc:
        STATUS = {"state":"error", "message":f"ວິເຄາະ Editing Style ບໍ່ສຳເລັດ: {str(exc)[:500]}", "progress":0, "files":[]}

def pack_capcut_choice(kind, source_folder, source_draft, ids):
    payload = {"kind":kind, "project":source_folder.name, "timeline":source_draft.parent.name, "ids":list(ids)}
    return "cc:" + base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")

def pack_local_capcut_choice(kind, path):
    """Carry only a path relative to CapCut's own User Data directory."""
    payload = {"kind":kind, "path":str(path.relative_to(CAPCUT_USER_DATA))}
    return "local:" + base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")

def unpack_local_capcut_choice(value, kind):
    value = str(value or "")
    if not value.startswith("local:"): return None
    try:
        encoded = value[6:] + "=" * (-len(value[6:]) % 4)
        choice = json.loads(base64.urlsafe_b64decode(encoded).decode())
        relative = Path(choice["path"])
        if choice.get("kind") != kind or relative.is_absolute() or ".." in relative.parts: return None
        path = CAPCUT_USER_DATA / relative
        return path if path.is_file() else None
    except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return None

def unpack_capcut_choice(value, kind):
    """Decode a style source selected in the UI; unknown/old values are safe no-ops.

    A saved animation can outlive the source project after CapCut moves that
    project to its recycle bin.  The downloaded animation definition is still
    valid, so it may be read from that exact archived timeline as a fallback.
    """
    value = str(value or "")
    if not value.startswith("cc:"): return None
    try:
        encoded = value[3:] + "=" * (-len(value[3:]) % 4)
        choice = json.loads(base64.urlsafe_b64decode(encoded).decode())
        if choice.get("kind") != kind or not isinstance(choice.get("ids"), list): return None
        project_name, timeline_id = str(choice["project"]), str(choice["timeline"])
        try:
            _, draft = capcut_draft_from_id(f"{project_name}::{timeline_id}")
        except RuntimeError:
            if any(part in project_name for part in ("/", "\\", "..")) or not re.fullmatch(r"[A-Za-z0-9-]+", timeline_id):
                return None
            archived = CAPCUT_ROOT / ".recycle_bin" / project_name / "Timelines" / timeline_id / "draft_info.json"
            if not archived.is_file(): return None
            draft = archived
        return draft, [str(item) for item in choice["ids"]]
    except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return None

def style_entries_from_draft(folder, draft):
    """Extract source-authoring effects exactly as CapCut serialized them."""
    data = json.loads(draft.read_text(encoding="utf-8"))
    try:
        _, _, _, segment, _ = primary_video_segment(data)
        references = set(segment.get("extra_material_refs", []))
    except RuntimeError:
        references = set()
    effects = {item.get("id"):item for item in data.get("materials", {}).get("effects", []) if item.get("id")}
    adjustment = [(item_id, item) for item_id, item in effects.items() if item_id in references and (item.get("sub_type") == "manual_stretch" or item.get("type") in {"brightness", "contrast", "saturation", "highlight", "shadow", "exposure", "temperature", "white", "black"})]
    if not adjustment:
        adjustment = [(item_id, item) for item_id, item in effects.items() if item.get("sub_type") == "manual_stretch"]
    retouch = [(item_id, item) for item_id, item in effects.items() if item_id in references and item.get("type") in {"figure", "makeup_root"}]
    result = {"adjustment":[], "retouch":[], "entry":[], "exit":[]}
    if adjustment:
        result["adjustment"].append({"id":pack_capcut_choice("effect", folder, draft, [item_id for item_id, _ in adjustment]), "label":f"Yours — {folder.name}"})
    if retouch:
        result["retouch"].append({"id":pack_capcut_choice("effect", folder, draft, [item_id for item_id, _ in retouch]), "label":f"Face preset — {folder.name}"})
    seen = set()
    for material in data.get("materials", {}).get("material_animations", []):
        for animation in material.get("animations", []):
            animation_id, direction = animation.get("id"), animation.get("type")
            if not animation_id or direction not in {"in", "out"} or (direction, animation_id) in seen: continue
            seen.add((direction, animation_id))
            result["entry" if direction == "in" else "exit"].append({"id":pack_capcut_choice("animation", folder, draft, [animation_id]), "label":f"{animation.get('name') or animation_id} — {folder.name}"})
    return result

def cached_adjustment_presets():
    """Read the exact named presets shown under CapCut Adjustment > Yours."""
    result, seen = [], set()
    if not CAPCUT_ADJUSTMENT_CACHE.is_dir(): return result
    for path in sorted(CAPCUT_ADJUSTMENT_CACHE.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name = str(data.get("name", "")).strip()
            if not name or not isinstance(data.get("adjust"), dict): continue
            fingerprint = (name, json.dumps(data["adjust"], sort_keys=True))
            if fingerprint in seen: continue
            seen.add(fingerprint)
            result.append({"id":pack_local_capcut_choice("adjustment", path), "label":f"Yours — {name}"})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return result

def cached_face_presets():
    """Read the named Face presets from CapCut's official local preset store."""
    result = []
    if not CAPCUT_FACE_PRESETS.is_dir(): return result
    for path in sorted(CAPCUT_FACE_PRESETS.glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not str(data.get("name", "")).strip() or not isinstance(data.get("auto_beauty"), list): continue
            result.append({"id":pack_local_capcut_choice("face", path), "label":f"Face preset — {data['name']}"})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return result

def capcut_style_options(capcut_id):
    """Read saved styles directly from all CapCut originals on this Mac.

    A selected timeline is listed first.  If it has no saved preset, JodSub
    also presents the user's other original CapCut timelines and imports the
    exact material only into the new copy when selected.
    """
    folder, selected_draft = capcut_draft_from_id(capcut_id)
    catalog = {"adjustment":[], "retouch":[], "entry":[], "exit":[]}
    catalog["adjustment"].extend(cached_adjustment_presets())
    catalog["retouch"].extend(cached_face_presets())
    ordered = [(folder, selected_draft)] + [(other_folder, other_draft) for other_folder, other_draft in capcut_drafts() or [] if other_draft != selected_draft]
    for source_folder, draft in ordered:
        entries = style_entries_from_draft(source_folder, draft)
        for key in catalog: catalog[key].extend(entries[key])
    for key, entries in catalog.items():
        deduped, labels = [], set()
        for item in entries:
            # CapCut projects can contain several historical timelines with
            # the same downloaded preset. One clear menu entry is enough.
            if item["label"] in labels: continue
            labels.add(item["label"]); deduped.append(item)
        catalog[key] = deduped
    return catalog

def import_effect_choice(data, selection):
    adjustment_path = unpack_local_capcut_choice(selection, "adjustment")
    if adjustment_path:
        return import_cached_adjustment(data, adjustment_path)
    face_path = unpack_local_capcut_choice(selection, "face")
    if face_path:
        return import_cached_face_preset(data, face_path)
    choice = unpack_capcut_choice(selection, "effect")
    if not choice: return [entry for entry in str(selection or "").split("|") if entry]
    draft, ids = choice
    source = json.loads(draft.read_text(encoding="utf-8"))
    source_effects = {item.get("id"):item for item in source.get("materials", {}).get("effects", [])}
    destination = data.setdefault("materials", {}).setdefault("effects", [])
    present = {item.get("id") for item in destination}
    for item_id in ids:
        if item_id not in source_effects: raise RuntimeError("ບໍ່ພົບ preset ທີ່ເລືອກໃນ CapCut.")
        if item_id not in present: destination.append(copy.deepcopy(source_effects[item_id])); present.add(item_id)
    return ids

def effect_templates():
    """Use actual CapCut effect JSON as a schema template; never invent it."""
    manual, face = {}, {}
    # A clean source project can legitimately have no Adjustment/Retouch
    # effects yet.  In that case, reuse only the JSON *shape* saved by a
    # previous JodSub copy made by this same installed CapCut version.  The
    # preset values still come from the user's selected Yours/Face file.
    # This avoids inventing an undocumented CapCut effect schema while also
    # allowing a minimal template project to receive local presets.
    drafts, seen = [], set()
    for _, draft in capcut_drafts() or []:
        if draft not in seen: drafts.append(draft); seen.add(draft)
    if CAPCUT_ROOT.is_dir():
        for draft in CAPCUT_ROOT.glob("JodSub-*/Timelines/*/draft_info.json"):
            if draft not in seen: drafts.append(draft); seen.add(draft)
        # CapCut keeps older user-made projects in its recycle area.  It is
        # read-only here and is used solely as a last-resort schema reference
        # when the active template is deliberately bare.
        for draft in (CAPCUT_ROOT / ".recycle_bin").glob("*/Timelines/*/draft_info.json"):
            if draft not in seen: drafts.append(draft); seen.add(draft)
    for draft in drafts:
        try:
            data = json.loads(draft.read_text(encoding="utf-8"))
            for item in data.get("materials", {}).get("effects", []):
                if item.get("sub_type") == "manual_stretch": manual.setdefault(item.get("type"), item)
                if item.get("type") in {"figure", "makeup_root"}:
                    face.setdefault((str(item.get("resource_id", "")), str(item.get("third_resource_id", ""))), item)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return manual, face

def append_effects(data, effects):
    destination = data.setdefault("materials", {}).setdefault("effects", [])
    ids = []
    for effect in effects:
        effect = copy.deepcopy(effect); effect["id"] = str(uuid.uuid4()).upper()
        destination.append(effect); ids.append(effect["id"])
    return ids

def import_cached_adjustment(data, path):
    """Turn a CapCut Yours preset into its exact saved adjustment effects."""
    preset = json.loads(path.read_text(encoding="utf-8"))
    manual, _ = effect_templates()
    field_types = {"brightness_value":"brightness", "contrast_value":"contrast", "saturation_value":"saturation", "highlight_value":"highlight", "shadow_value":"shadow", "white_value":"white", "black_value":"black", "temperature_value":"temperature"}
    effects = []
    for field, effect_type in field_types.items():
        if field not in preset.get("adjust", {}) or effect_type not in manual: continue
        effect = copy.deepcopy(manual[effect_type]); effect["value"] = float(preset["adjust"][field]); effect["name"] = str(preset.get("name", ""))
        effects.append(effect)
    if not effects: raise RuntimeError("ບໍ່ພົບຮູບແບບ Adjustment ຈາກ CapCut ເພື່ອໃຊ້ preset ນີ້.")
    return append_effects(data, effects)

def import_cached_face_preset(data, path):
    """Turn CapCut's saved Face preset into native figure effects."""
    preset = json.loads(path.read_text(encoding="utf-8"))
    _, templates = effect_templates()
    effects = []
    for item in preset.get("auto_beauty", []):
        template = templates.get((str(item.get("resource_id", "")), str(item.get("third_resource_id", ""))))
        if not template: continue
        effect = copy.deepcopy(template); effect["value"] = float(item.get("value", 0)); effect["name"] = str(item.get("name", "")); effect["beauty_face_auto_preset_id"] = str(preset.get("id", "")); effects.append(effect)
    skin = preset.get("skin_color_info", {})
    template = templates.get((str(skin.get("resource_id", "")), str(skin.get("third_resource_id", ""))))
    if template:
        effect = copy.deepcopy(template); effect["name"] = str(skin.get("name", "")); effect["adjust_params"] = copy.deepcopy(skin.get("adjust_params", [])); effect["beauty_face_auto_preset_id"] = str(preset.get("id", "")); effects.append(effect)
    if not effects: raise RuntimeError("ບໍ່ພົບ Face effect ທີ່ບັນທຶກໄວ້ໃນ CapCut ເພື່ອໃຊ້ preset ນີ້.")
    return append_effects(data, effects)

def apply_selected_video_styles(data, config, video_material_id=None):
    """Attach the selected saved Yours/Face bundles to every video shot."""
    selected = []
    for value in (config.get("adjustment", "inherit"), config.get("retouch", "inherit")):
        if value and value != "inherit": selected.extend(import_effect_choice(data, value))
    available = {item.get("id") for item in data.get("materials", {}).get("effects", [])}
    selected = [item for item in selected if item in available]
    if not selected: return []
    for track in data.get("tracks", []):
        if track.get("type") != "video": continue
        for segment in track.get("segments", []):
            refs = segment.setdefault("extra_material_refs", [])
            for item in selected:
                if item not in refs: refs.append(item)
            # CapCut ignores effect references when this per-segment switch is
            # disabled (common in clean/template timelines).
            segment["enable_adjust"] = True
    return selected

def media_duration(path):
    try:
        output = subprocess.run([ffmpeg(), "-i", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True).stderr
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
        if match: return max(100000, int((int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))) * 1000000))
    except Exception: return 800000
    return 800000

def capcut_audio_template():
    """Obtain a real audio track shape from this Mac's installed CapCut version."""
    drafts = []
    for pattern in ("*/Timelines/*/draft_info.json", "JodSub-*/Timelines/*/draft_info.json", ".recycle_bin/*/Timelines/*/draft_info.json"):
        drafts.extend(CAPCUT_ROOT.glob(pattern))
    for draft in drafts:
        try:
            data = json.loads(draft.read_text(encoding="utf-8"))
            track = next((item for item in data.get("tracks", []) if item.get("type") == "audio" and item.get("segments")), None)
            if track and data.get("materials", {}).get("audios"):
                return copy.deepcopy(track), copy.deepcopy(data["materials"]["audios"][0]), copy.deepcopy(track["segments"][0])
        except (OSError, ValueError, TypeError):
            continue
    raise RuntimeError("ບໍ່ພົບ audio template ໃນ CapCut. ສ້າງ audio 1 ອັນໃນ project ໃດໜຶ່ງ ແລ້ວບັນທຶກກ່ອນ.")

def capcut_ready_sfx(event, destination, number):
    source = Path(event["file"])
    if not source.is_file(): raise RuntimeError(f"ບໍ່ພົບ SFX: {source.name}")
    folder = destination / "JodSub SFX"; folder.mkdir(exist_ok=True)
    output = folder / f"{number:02d}-{clean(source.stem)}.m4a"
    subprocess.run([ffmpeg(), "-y", "-i", str(source), "-vn", "-af", "apad=pad_dur=1,atrim=duration=1", "-t", "1", "-c:a", "aac", "-b:a", "128k", str(output)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if not output.is_file() or output.stat().st_size < 256: raise RuntimeError(f"ສ້າງ SFX ສຳລັບ CapCut ບໍ່ສຳເລັດ: {source.name}")
    return output

def update_capcut_text(material, text):
    material["id"] = str(uuid.uuid4()).upper()
    material["name"] = "JodSub subtitle"
    try:
        content = json.loads(material.get("content", "{}"))
        content["text"] = text
        for item in content.get("styles", []): item["range"] = [0, len(text)]
        material["content"] = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError, json.JSONDecodeError):
        material["content"] = material.get("content", "")
    return material

def capcut_color_value(value, fallback=(1.0, 0.25, 0.0)):
    """Convert the UI hex colour to CapCut's normalized RGB array."""
    value = str(value or "").strip().lstrip("#")
    if len(value) != 6 or any(char not in "0123456789abcdefABCDEF" for char in value): return list(fallback)
    return [round(int(value[index:index + 2], 16) / 255.0, 4) for index in (0, 2, 4)]

def update_capcut_karaoke_text(material, text, active_start, active_end, highlight_color="#FF4000"):
    """A native styled-text overlay for one spoken word.

    The base subtitle stays visible underneath.  This overlay uses the real
    CapCut `styles/range/fill` structure cloned from the selected text object,
    so it remains editable as ordinary text in the destination project.
    """
    material = update_capcut_text(material, text)
    material["name"] = "JodSub karaoke word"
    try:
        content = json.loads(material.get("content", "{}"))
        styles = content.get("styles", [])
        if not styles: return material
        base = copy.deepcopy(styles[0]); base["range"] = [0, len(text)]
        highlight = copy.deepcopy(base); highlight["range"] = [max(0, active_start), min(len(text), active_end)]
        highlight["fill"] = {"alpha":1.0, "content":{"render_type":"solid", "solid":{"alpha":1.0, "color":capcut_color_value(highlight_color)}}}
        highlight["useLetterColor"] = True
        content["styles"] = [base, highlight]
        material["content"] = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return material

def capcut_text_animation(materials=None, animation_id="", direction=""):
    """CapCut 9.x requires a companion material for native text segments.

    When a saved local animation is selected, clone its exact downloaded
    definition rather than manufacturing an uninstalled animation ID.
    """
    chosen = None
    if materials and animation_id:
        for material in materials.get("material_animations", []):
            for animation in material.get("animations", []):
                if str(animation.get("id")) == str(animation_id) and animation.get("type") == direction:
                    chosen = copy.deepcopy(animation); break
            if chosen: break
    return {"id":str(uuid.uuid4()).upper(), "type":"sticker_animation", "animations":[chosen] if chosen else [], "multi_language_current":"none"}

def import_animation_choice(data, selection, direction):
    """Copy a downloaded local CapCut animation into the destination draft."""
    choice = unpack_capcut_choice(selection, "animation")
    if not choice: return str(selection or "")
    draft, ids = choice
    animation_id = ids[0] if ids else ""
    source = json.loads(draft.read_text(encoding="utf-8"))
    chosen = None
    for material in source.get("materials", {}).get("material_animations", []):
        for animation in material.get("animations", []):
            if str(animation.get("id")) == animation_id and animation.get("type") == direction:
                chosen = copy.deepcopy(animation); break
        if chosen: break
    if not chosen: raise RuntimeError("ບໍ່ພົບ Animation ທີ່ເລືອກໃນ CapCut.")
    materials = data.setdefault("materials", {})
    materials.setdefault("material_animations", []).append({"id":str(uuid.uuid4()).upper(), "type":"sticker_animation", "animations":[chosen], "multi_language_current":"none"})
    return animation_id

def apply_timeline_edge_animations(data, video_material_id, entry_animation, exit_animation):
    """Put animation on the first and last *video shot*, never on subtitles."""
    candidates = []
    for track in data.get("tracks", []):
        if track.get("type") != "video": continue
        for segment in track.get("segments", []):
            if segment.get("material_id") == video_material_id:
                candidates.append(segment)
    if not candidates: return []
    candidates.sort(key=lambda segment: int(segment.get("target_timerange", {}).get("start", 0)))
    materials = data.setdefault("materials", {})
    applied = []
    for segment, animation_id, direction in ((candidates[0], entry_animation, "in"), (candidates[-1], exit_animation, "out")):
        if not animation_id: continue
        animation_id = import_animation_choice(data, animation_id, direction)
        material = capcut_text_animation(materials, animation_id, direction)
        # An exact local animation definition is copied, not an invented ID.
        if not material["animations"]: continue
        materials.setdefault("material_animations", []).append(material)
        refs = segment.setdefault("extra_material_refs", [])
        if material["id"] not in refs: refs.append(material["id"])
        applied.append((direction, material["id"]))
    return applied

def handoff_capcut(project_id, capcut_id):
    global STATUS
    try:
        if subprocess.run(["pgrep", "-x", "CapCut"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError("ກະລຸນາປິດ CapCut ກ່ອນ. JodSub ຈະບໍ່ຂຽນທັບໃນຂະນະທີ່ CapCut ເປີດຢູ່.")
        source, source_draft = capcut_draft_from_id(capcut_id)
        project = load_project(project_id)
        project.setdefault("config", {})
        words_per_caption = int(project["config"].get("wordsPerCaption", 3))
        validate_provider_word_timing(project)
        # Speaker-labelled captions are already grouped at word level; running
        # the normal formatter again would merge different speakers together.
        if not any(item.get("speaker") for item in project.get("captions", [])):
            project["captions"] = format_captions(project.get("captions", []), words_per_caption)
        else:
            project["captions"] = copy.deepcopy(project.get("captions", []))
        if not project["captions"]: raise RuntimeError("ບໍ່ມີ subtitle ສຳລັບສົ່ງເຂົ້າ CapCut.")
        srt = EXPORTS / f"{clean(project['name'])}.srt"; write_srt(project["captions"], srt); project["srt_file"] = srt.name
        requested_sfx = max(0, min(20, int(project["config"].get("sfxCount", 0))))
        if requested_sfx:
            # Always derive SFX positions from the current caption clock.  A
            # project may have been re-aligned since its previous SFX pass.
            project["sfx_events"] = sfx_events(project["captions"], requested_sfx, project_id)
        # Projects saved before this version may reference variable-length SFX.
        for event in project.get("sfx_events", []):
            source_sfx = Path(event.get("file", ""))
            if source_sfx.is_file(): event["file"] = str(one_second_sfx(source_sfx))
        save_project(project)
        capcut_captions = copy.deepcopy(project["captions"])
        capcut_sfx_events = copy.deepcopy(project.get("sfx_events", []))
        handoff_srt = project["srt_file"]
        STATUS = {"state":"working", "message":f"ກຳລັງສ້າງ Timeline 2 ຈາກ project {source.name} / timeline {source_draft.parent.name[:8]}…", "progress":20, "files":[]}
        # Read the selected timeline as the template, but write the generated
        # result into a new sibling timeline inside the same project.  The
        # original project and its selected timeline remain untouched.
        destination = source
        source_draft_path = source_draft
        if not source_draft_path.is_file(): raise RuntimeError("ບໍ່ພົບ timeline ທີ່ເລືອກໃນ CapCut project.")
        data = json.loads(source_draft_path.read_text(encoding="utf-8"))
        capcut_captions = map_captions_to_capcut_timeline(capcut_captions, data)
        relative_draft = source_draft_path.relative_to(source)
        if "Timelines" not in relative_draft.parts:
            raise RuntimeError("project ນີ້ບໍ່ມີໂຟນເດີ Timelines ສຳລັບສ້າງ Timeline 2.")
        # Prefer the empty timeline the user created in CapCut (usually
        # "ไทม์ไลน์ 02") instead of creating another hidden timeline.
        timeline_root = destination / "Timelines"
        empty_timelines = []
        layout_timeline2 = None
        layout_path = destination / "timeline_layout.json"
        if layout_path.is_file():
            try:
                layout_data = json.loads(layout_path.read_text(encoding="utf-8"))
                dock = layout_data.get("dockItems", [{}])[0]
                ids = dock.get("timelineIds", [])
                if len(ids) > 1:
                    candidate = timeline_root / str(ids[1]) / "draft_info.json"
                    if candidate.is_file() and candidate.resolve() != source_draft_path.resolve():
                        layout_timeline2 = candidate
            except (OSError, ValueError, TypeError, IndexError):
                pass
        for candidate in timeline_root.glob("*/draft_info.json"):
            if candidate.resolve() == source_draft_path.resolve():
                continue
            try:
                candidate_data = json.loads(candidate.read_text(encoding="utf-8"))
                if not candidate_data.get("tracks"):
                    empty_timelines.append(candidate)
            except (OSError, ValueError, TypeError):
                continue
        timeline2 = layout_timeline2 or (sorted(empty_timelines, key=lambda item: item.stat().st_mtime, reverse=True)[0] if empty_timelines else timeline_root / str(uuid.uuid4()).upper() / "draft_info.json")
        timeline2.parent.mkdir(parents=True, exist_ok=True)
        data["id"] = timeline2.parent.name
        draft = timeline2
        layout = destination / "timeline_layout.json"
        if layout.is_file():
            try:
                layout_data = json.loads(layout.read_text(encoding="utf-8"))
                dock = (layout_data.setdefault("dockItems", [{}])[0])
                ids = dock.setdefault("timelineIds", [])
                names = dock.setdefault("timelineNames", [])
                if timeline2.parent.name not in ids:
                    ids.append(timeline2.parent.name)
                    names.append("JodSub Edit — Timeline 2")
                layout.write_text(json.dumps(layout_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            except (OSError, ValueError, TypeError):
                pass
        materials, tracks = data.setdefault("materials", {}), data.setdefault("tracks", [])
        source_text_track = next((track for track in tracks if track.get("type") == "text" and track.get("segments")), None)
        if not source_text_track or not materials.get("texts"):
            # A source project no longer needs a manually-created Text clip.
            # Reuse a known-good JodSub text object as an internal template.
            donor = None
            # CapCut stores draft_info.json below each project's Timelines
            # directory, not necessarily at the project root.  Search
            # recursively so a valid donor is found on every installation.
            for candidate in sorted(CAPCUT_ROOT.rglob("draft_info.json")):
                try:
                    donor_data = json.loads(candidate.read_text(encoding="utf-8"))
                    donor_track = next((t for t in donor_data.get("tracks", []) if t.get("type") == "text" and t.get("segments")), None)
                    donor_text = donor_data.get("materials", {}).get("texts", [])
                    if donor_track and donor_text:
                        donor = (donor_track, donor_text[0]); break
                except Exception: continue
            if not donor:
                raise RuntimeError("ບໍ່ພົບ text template ພາຍໃນ JodSub ສຳລັບສ້າງ subtitle.")
            source_text_track = copy.deepcopy(donor[0]); source_text_track["segments"] = [copy.deepcopy(donor[0]["segments"][0])]
            materials.setdefault("texts", []).append(copy.deepcopy(donor[1]))
        # A retry against an older JodSub copy can already contain generated
        # tracks.  Remove only those generated layers before adding the fresh
        # result; otherwise CapCut keeps displaying the stale short subtitle
        # track and verification may read that one instead of the new track.
        generated_track_names = {"JodSub subtitles", "JodSub karaoke", "JodSub SFX"}
        tracks[:] = [track for track in tracks if track.get("name") not in generated_track_names]
        _, _, _, primary_video_segment_data, primary_video_material = primary_video_segment(data)
        # `primary_video_segment_data` is only the first shot (12.4s in the
        # affected project), not the complete video.  Use the end of all video
        # segments so subtitles are never clipped at the first cut.
        # Each CapCut shot may have a different material id.  The timeline
        # boundary must therefore be computed from every video segment.
        primary_timeline_end = capcut_video_timeline_end(data)
        if not primary_timeline_end:
            primary_timeline_end = int(primary_video_segment_data.get("target_timerange", {}).get("start", 0)) + int(primary_video_segment_data.get("target_timerange", {}).get("duration", 0))
        capcut_captions = constrain_captions_to_timeline(capcut_captions, primary_timeline_end)
        if not capcut_captions:
            raise RuntimeError("subtitle ຢູ່ນອກຂອບເວລາຂອງ video ໃນ CapCut.")
        # SFX must use the same final, bounded clock as the copied subtitles.
        capcut_sfx_events = sfx_events(capcut_captions, requested_sfx, project_id) if requested_sfx else []
        cut = None
        if enabled_config_flag(project["config"].get("deadAir", False)):
            STATUS.update({"message":"ກຳລັງວິເຄາະ Dead Air ແລະຕັດເປັນ shot…", "progress":35})
            cut = apply_dead_air_cuts(data, project["files"]["video"], project["config"])
            if cut:
                capcut_captions = captions_after_cuts(project["captions"], cut)
                if not capcut_captions: raise RuntimeError("Dead Air cut ຈະຕັດ subtitle ອອກທັງໝົດ; ບໍ່ໄດ້ສ້າງ copy.")
                capcut_sfx_events = sfx_events(capcut_captions, requested_sfx, project_id) if requested_sfx else []
                cut_srt = EXPORTS / f"{clean(project['name'])}-capcut-cut.srt"; write_srt(capcut_captions, cut_srt); handoff_srt = cut_srt.name
        # Recompute the actual destination video end after optional cuts and
        # validate coverage against that end.  This is deliberately before any
        # text track is written, so an incomplete copy can never be reported as
        # successful.
        caption_timeline_end = capcut_video_timeline_end(data)
        if not caption_timeline_end:
            caption_timeline_end = primary_timeline_end
        capcut_captions = constrain_captions_to_timeline(capcut_captions, caption_timeline_end)
        ensure_caption_coverage(capcut_captions, caption_timeline_end)
        selected_styles = apply_selected_video_styles(data, project["config"], cut["video_material_id"] if cut else primary_video_material["id"])
        # The user's entry/exit choices belong to the first and last editable
        # video shot in the timeline.  Subtitle and karaoke segments remain
        # clean text layers so their timing is never delayed by animation.
        applied_video_animations = apply_timeline_edge_animations(
            data,
            cut["video_material_id"] if cut else primary_video_material["id"],
            str(project["config"].get("entryAnimation", "")),
            str(project["config"].get("exitAnimation", "")),
        )
        segment_template = source_text_track["segments"][0]
        material_template = next((item for item in materials["texts"] if item.get("id") == segment_template.get("material_id")), materials["texts"][0])
        speaker_tracks = {}
        for caption_index, caption in enumerate(capcut_captions):
            speaker_key = str(caption.get("speaker", "")).strip() or "default"
            if speaker_key not in speaker_tracks:
                track = {"attribute":0, "flag":0, "id":str(uuid.uuid4()).upper(), "is_default_name":True, "name":"JodSub subtitles" if not speaker_tracks else f"JodSub subtitles — {speaker_key}", "segments":[], "type":"text"}
                speaker_tracks[speaker_key] = (len(tracks), track); tracks.append(track)
            track_position, text_track = speaker_tracks[speaker_key]
            material = update_capcut_text(copy.deepcopy(material_template), str(caption["text"]))
            try:
                content = json.loads(material.get("content", "{}")); size = 20
                for style in content.get("styles", []):
                    style["size"] = float(size); style.setdefault("font", {})["path"] = str(Path.home() / "Library/Containers/com.lemon.lvoverseas/Data/Library/Fonts/Nuanta-Medium.ttf")
                material["content"] = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            except (ValueError, TypeError, json.JSONDecodeError): pass
            materials["texts"].append(material)
            segment = copy.deepcopy(segment_template); segment["id"] = str(uuid.uuid4()).upper(); segment["material_id"] = material["id"]
            segment["target_timerange"] = {"start": int(float(caption["start"]) * 1000000), "duration": max(100000, int((float(caption["end"]) - float(caption["start"])) * 1000000))}
            animation = capcut_text_animation(); materials.setdefault("material_animations", []).append(animation); segment["extra_material_refs"] = [animation["id"]]
            segment["track_render_index"] = track_position
            text_track["segments"].append(segment)
        # Optional second editable native-text track: it overlays only the
        # spoken word and changes its fill colour.  In single-colour mode we
        # omit this layer entirely, leaving the original CapCut text style.
        karaoke_enabled = str(project["config"].get("karaokeMode", "highlight")) == "highlight"
        karaoke_track = None; karaoke_words = 0
        if karaoke_enabled:
            karaoke_position = len(tracks)
            karaoke_track = {"attribute":0, "flag":0, "id":str(uuid.uuid4()).upper(), "is_default_name":True, "name":"JodSub karaoke", "segments":[], "type":"text"}
            tracks.append(karaoke_track)
            for caption in capcut_captions:
                word_items = timed_words(caption)
                whole = str(caption["text"]); cursor = 0
                for word in word_items:
                    token = str(word["text"]); begin = whole.find(token, cursor)
                    if begin < 0: continue
                    finish = begin + len(token); cursor = finish
                    word_start, word_end = float(word["start"]), float(word["end"])
                    # Preserve the acoustic model's exact word window.  The
                    # former forced 40 ms minimum extended short words into
                    # the following word and made the colour/text layer lead
                    # or overlap even though its stored onset was correct.
                    # Karaoke highlight is controlled by its own picker; the
                    # speaker colours belong to the base layer only.
                    colour = project["config"].get("subtitleColor", "#FFD400")
                    material = update_capcut_karaoke_text(copy.deepcopy(material_template), whole, begin, finish, colour)
                    # Keep the karaoke overlay at the same word-count size as
                    # its editable base subtitle.  Without this, CapCut may
                    # fall back to the template's small default (often 20).
                    try:
                        content = json.loads(material.get("content", "{}"))
                        size = 20
                        for style in content.get("styles", []):
                            style["size"] = float(size)
                            style.setdefault("font", {})["path"] = str(Path.home() / "Library/Containers/com.lemon.lvoverseas/Data/Library/Fonts/Nuanta-Medium.ttf")
                        material["content"] = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                    except (ValueError, TypeError, json.JSONDecodeError):
                        pass
                    materials["texts"].append(material)
                    segment = copy.deepcopy(segment_template); segment["id"] = str(uuid.uuid4()).upper(); segment["material_id"] = material["id"]
                    segment["target_timerange"] = {"start":int(word_start * 1000000), "duration":max(1000, int((word_end - word_start) * 1000000))}
                    animation = capcut_text_animation(); materials.setdefault("material_animations", []).append(animation); segment["extra_material_refs"] = [animation["id"]]
                    segment["track_render_index"] = karaoke_position; karaoke_track["segments"].append(segment); karaoke_words += 1
        inserted_sfx = 0
        if capcut_sfx_events:
            audio_track = next((track for track in tracks if track.get("type") == "audio" and track.get("segments")), None)
            if audio_track and materials.get("audios"):
                track_template, audio_template, audio_segment_template = audio_track, materials["audios"][0], audio_track["segments"][0]
            else:
                track_template, audio_template, audio_segment_template = capcut_audio_template()
            audio_track_position = len(tracks)
            sfx_track = {key: copy.deepcopy(value) for key, value in track_template.items() if key != "segments"}
            sfx_track["id"] = str(uuid.uuid4()).upper(); sfx_track["name"] = "JodSub SFX"; sfx_track["segments"] = []; tracks.append(sfx_track)
            for number, event in enumerate(capcut_sfx_events, 1):
                clip_path = capcut_ready_sfx(event, destination, number)
                audio = copy.deepcopy(audio_template); audio["id"] = str(uuid.uuid4()).upper(); audio["name"] = clip_path.stem; audio["path"] = str(clip_path); audio["duration"] = 1000000; audio["is_ugc"] = True; audio["source_platform"] = 0; audio["resource_id"] = ""; audio["effect_id"] = ""; materials.setdefault("audios", []).append(audio)
                segment = copy.deepcopy(audio_segment_template); segment["id"] = str(uuid.uuid4()).upper(); segment["material_id"] = audio["id"]; segment["extra_material_refs"] = []; segment["source_timerange"] = {"start":0,"duration":1000000}; segment["target_timerange"] = {"start":int(float(event["time"])*1000000),"duration":1000000}; segment["volume"] = 10 ** (-15 / 20); segment["track_render_index"] = audio_track_position; sfx_track["segments"].append(segment); inserted_sfx += 1
            if not inserted_sfx: raise RuntimeError("ເລືອກ SFX ແລ້ວ ແຕ່ບໍ່ສາມາດວາງລົງ CapCut ໄດ້.")
        draft.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        # Keep the original timeline untouched and publish the generated copy
        # as a second CapCut timeline in the same project folder.  CapCut
        # discovers these timelines from Timelines/<uuid>/draft_info.json.
        timeline2.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        meta = destination / "draft_meta_info.json"
        if meta.exists() and destination != source:
            metadata = json.loads(meta.read_text(encoding="utf-8")); metadata["draft_name"] = destination.name; metadata["draft_id"] = str(uuid.uuid4()).upper(); meta.write_text(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        checked = json.loads(draft.read_text(encoding="utf-8"))
        verified_text_track = next((track for track in checked.get("tracks", []) if track.get("name") == "JodSub subtitles"), None)
        if not verified_text_track or len(verified_text_track.get("segments", [])) != len(capcut_captions):
            raise RuntimeError("ກວດສອບ subtitle ໃນ CapCut copy ບໍ່ຜ່ານ.")
        verified_subtitle_end = max(
            (int(segment.get("target_timerange", {}).get("start", 0)) + int(segment.get("target_timerange", {}).get("duration", 0))
             for segment in verified_text_track.get("segments", [])),
            default=0,
        )
        expected_subtitle_end = int(max(float(item.get("end", 0.0)) for item in capcut_captions) * 1000000)
        if abs(verified_subtitle_end - expected_subtitle_end) > 150000:
            raise RuntimeError("ກວດສອບຂອບເວລາ subtitle ໃນ CapCut copy ບໍ່ຜ່ານ.")
        verified_karaoke_track = next((track for track in checked.get("tracks", []) if track.get("name") == "JodSub karaoke"), None)
        if karaoke_enabled and (not verified_karaoke_track or len(verified_karaoke_track.get("segments", [])) != karaoke_words):
            raise RuntimeError("ກວດສອບ layer ເນັ້ນຄຳໃນ CapCut copy ບໍ່ຜ່ານ.")
        # Verify the selected preset bundles and timeline-edge animation were
        # actually serialized in the destination draft before reporting done.
        checked_video_refs = [ref for track in checked.get("tracks", []) if track.get("type") == "video" for segment in track.get("segments", []) for ref in segment.get("extra_material_refs", [])]
        if any(style not in checked_video_refs for style in selected_styles):
            raise RuntimeError("ກວດສອບ Yours/Face preset ໃນ CapCut copy ບໍ່ຜ່ານ.")
        checked_animation_ids = {item.get("id") for item in checked.get("materials", {}).get("material_animations", [])}
        if any(animation_id not in checked_animation_ids for _, animation_id in applied_video_animations):
            raise RuntimeError("ກວດສອບ Animation ຂອງ shot ໃນ CapCut copy ບໍ່ຜ່ານ.")
        created_sfx = [item for item in checked.get("materials", {}).get("audios", []) if str(item.get("path", "")).startswith(str(destination / "JodSub SFX"))]
        verified_sfx_track = next((track for track in checked.get("tracks", []) if track.get("name") == "JodSub SFX"), None)
        if len(created_sfx) < inserted_sfx or any(not Path(item["path"]).is_file() or item.get("duration") != 1000000 for item in created_sfx[-inserted_sfx:] if inserted_sfx) or (inserted_sfx and (not verified_sfx_track or len(verified_sfx_track.get("segments", [])) < inserted_sfx or any(segment.get("target_timerange", {}).get("duration") != 1000000 for segment in verified_sfx_track.get("segments", [])[-inserted_sfx:]))):
            raise RuntimeError("ກວດສອບ SFX ໃນ CapCut copy ບໍ່ຜ່ານ.")
        shots = cut["shots"] if cut else 0
        manifest = EXPORTS / f"{clean(project['name'])}-capcut-handoff.txt"; manifest.write_text(f"CapCut project copy: {destination}\nTimeline 2: {timeline2 if timeline2 else 'not available for this project layout'}\nCaptions: {len(capcut_captions)}\nSubtitle colour mode: {'word highlight' if karaoke_enabled else 'single colour'}\nKaraoke word overlays: {karaoke_words}\nSFX inserted (1 second each): {inserted_sfx}\nDead-air shots: {shots}\nSelected adjustment/retouch effects: {len(selected_styles)}\nAnimation applied to timeline edge shots: {len(applied_video_animations)}\n", encoding="utf-8")
        files = [manifest.name] + ([handoff_srt] if handoff_srt and (EXPORTS / handoff_srt).exists() else [])
        suffix = f"; Dead Air {shots} shot" if cut else ""
        colour_mode = "ປ່ຽນສີຕາມຄຳ" if karaoke_enabled else "ສີດຽວ"
        STATUS = {"state":"done", "message":f"ສ້າງ Timeline 2 ໃນ CapCut project {destination.name} ແລ້ວ; subtitle {colour_mode}; SFX {inserted_sfx} ສຽງ{suffix}.", "progress":100, "files":files}
    except Exception as exc:
        STATUS = {"state":"error", "message":f"CapCut handoff ບໍ່ສຳເລັດ: {str(exc)[:600]}", "progress":0, "files":[]}

def render(project_id):
    global STATUS
    project = load_project(project_id); STATUS = {"state":"working","message":"ກຳລັງ render video…","progress":15,"files":[]}
    pulse = None
    try:
        base = clean(project["name"]); srt = EXPORTS / f"{base}.srt"; ass = EXPORTS / f"{base}.ass"; mp4 = EXPORTS / f"{base}.mp4"
        captions, config = project["captions"], project["config"]
        write_srt(captions, srt); write_ass(captions, config, ass)
        STATUS.update({"message":"ກຳລັງສ້າງ subtitle ແລະ mix ສຽງ (45%)", "progress":45})
        pulse = status_pulse("ກຳລັງ render video", 45, 94)
        source = project["files"]["video"]
        start, end = float(config.get("trimStart", 0)), float(config.get("trimEnd", 0))
        command = [ffmpeg(), "-y"]
        if start > 0: command += ["-ss", str(start)]
        command += ["-i", source]
        end_args = ["-to", str(end)] if end > start else []
        broll = project["files"].get("broll")
        if broll:
            command += ["-stream_loop", "-1", "-i", broll]
        music = project["files"].get("music")
        if music:
            command += ["-stream_loop", "-1", "-i", music]
        sfx = project["files"].get("sfx")
        if sfx: command += ["-i", sfx]
        automatic_sfx = [event for event in project.get("sfx_events", []) if Path(event.get("file", "")).is_file()]
        for event in automatic_sfx: command += ["-i", event["file"]]
        ass_filter = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        filters, video_label = [f"[0:v]ass='{ass_filter}'[sub]"], "[sub]"
        if broll:
            bstart = max(0, float(config.get("brollStart", 0)))
            bend = max(bstart + .1, float(config.get("brollEnd", bstart + 3)))
            filters.append(f"[1:v]scale=iw*0.42:-2[br]")
            filters.append(f"[sub][br]overlay=W-w-40:40:enable='between(t,{bstart},{bend})'[v]")
            video_label = "[v]"
        audio_inputs = ["[0:a]"]
        next_input = 2 if broll else 1
        if music:
            audio_inputs.append(f"[{next_input}:a]"); next_input += 1
        if sfx:
            audio_inputs.append(f"[{next_input}:a]"); next_input += 1
        for number, event in enumerate(automatic_sfx):
            delay = max(0, int(float(event["time"]) * 1000))
            filters.append(f"[{next_input + number}:a]adelay={delay}|{delay},volume=0.72[auto{number}]")
            audio_inputs.append(f"[auto{number}]")
        if len(audio_inputs) > 1:
            filters.append("".join(audio_inputs) + f"amix=inputs={len(audio_inputs)}:duration=first:normalize=0[a]")
            audio_label = "[a]"
        else:
            audio_label = "0:a?"
        command += ["-filter_complex", ";".join(filters), "-map", video_label, "-map", audio_label] + end_args
        command += ["-c:v", "libx264", "-preset", "medium", "-crf", str(config.get("crf", 20)), "-c:a", "aac", "-movflags", "+faststart", str(mp4)]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        pulse.set(); pulse = None
        STATUS = {"state":"done","message":"ສຳເລັດ — MP4, SRT ແລະ ASS ພ້ອມແລ້ວ.","progress":100,"files":[mp4.name,srt.name,ass.name]}
    except Exception as exc:
        if pulse: pulse.set()
        STATUS = {"state":"error","message":f"Render ບໍ່ສຳເລັດ: {str(exc)[:280]}","progress":0,"files":[]}

class App(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()
    def json(self, data, code=200):
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
    def do_GET(self):
        if self.path == "/api/status": return self.json(STATUS)
        if self.path == "/api/precision-model":
            size = OPENAI_WHISPER_V3_FILE.stat().st_size if OPENAI_WHISPER_V3_FILE.is_file() else 0
            return self.json({"whisperx":whisperx_v3_ready(), "openaiLargeV3":openai_whisper_v3_ready(), "downloadedBytes":size, "totalBytes":OPENAI_WHISPER_V3_SIZE})
        if self.path == "/api/fonts": return self.json({"fonts":fonts()})
        if self.path == "/api/projects": return self.json({"projects":[json.loads(p.read_text(encoding="utf-8")) for p in PROJECTS.glob("*.json")]})
        if self.path == "/api/capcut-projects": return self.json({"projects":capcut_projects()})
        if self.path == "/api/editing-style":
            return self.json({"profile":load_editing_profile(EDITING_STYLE_PROFILE)})
        if self.path == "/api/editing-style/rules":
            profile = load_editing_profile(EDITING_STYLE_PROFILE)
            return self.json({"rules":profile.get("removed_words", {})})
        if self.path.startswith("/api/capcut-styles/"):
            try: return self.json(capcut_style_options(unquote(self.path.removeprefix("/api/capcut-styles/").split("?", 1)[0])))
            except Exception as exc: return self.json({"error":str(exc)[:300]}, 400)
        if self.path == "/api/sfx": return self.json({"count":len(sfx_catalog()), "files":[item.name for item in sfx_catalog()]})
        if self.path == "/api/pixabay-search":
            length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length)); key = str(payload.get("key", "")).strip(); query = str(payload.get("query", "")).strip()
            if not key or not query: return self.json({"error":"ຕ້ອງມີ Pixabay API key ແລະຄຳຄົ້ນຫາ."}, 400)
            try:
                url = "https://pixabay.com/api/videos/?" + urlencode({"key":key, "q":query, "per_page":8, "safesearch":"true"})
                with urlopen_retry(url, timeout=20) as resp: data = json.loads(resp.read().decode())
                results = [{"id":x.get("id"), "duration":x.get("duration"), "preview":x.get("videos",{}).get("tiny",{}).get("url"), "url":x.get("pageURL")} for x in data.get("hits", [])]
                return self.json({"results":results})
            except Exception as exc: return self.json({"error":f"Pixabay ຄົ້ນຫາບໍ່ສຳເລັດ: {str(exc)[:240]}"}, 400)
        if self.path.startswith("/exports/"):
            self.path = self.path[1:]; return super().do_GET()
        self.path = "index.html"; return super().do_GET()
    def do_POST(self):
        global STATUS
        if self.path == "/api/render":
            length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length)); project_id = payload["id"]
            if STATUS["state"] == "working": return self.json({"error":"busy"},409)
            threading.Thread(target=render,args=(project_id,),daemon=True).start(); return self.json({"ok":True},202)
        if self.path == "/api/ai":
            length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length)); project_id = payload.get("id")
            if STATUS["state"] == "working": return self.json({"error":"busy"},409)
            if not project_id or not project_path(project_id).exists(): return self.json({"error":"project not found"},404)
            threading.Thread(target=ai_job,args=(project_id,payload.get("action"),payload),daemon=True).start(); return self.json({"ok":True},202)
        if self.path == "/api/gemini-models":
            length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length))
            try: return self.json({"models":gemini_models(payload.get("apiKey", ""))})
            except Exception as exc: return self.json({"error":str(exc)[:300]},400)
        if self.path == "/api/capcut":
            length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length)); project_id = payload.get("id")
            if STATUS["state"] == "working": return self.json({"error":"busy"},409)
            if not project_id or not project_path(project_id).exists(): return self.json({"error":"project not found"},404)
            threading.Thread(target=handoff_capcut,args=(project_id,payload.get("capcutId", "")),daemon=True).start(); return self.json({"ok":True},202)
        if self.path == "/api/editing-style":
            if STATUS["state"] == "working": return self.json({"error":"busy"},409)
            try:
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD":"POST","CONTENT_TYPE":self.headers.get("Content-Type")})
                capcut_id = str(form.getfirst("capcutId", "")).strip()
                if not capcut_id: return self.json({"error":"ກະລຸນາເລືອກ CapCut reference project."},400)
                reference_dir = EDITING_STYLE_ROOT / "references" / uuid.uuid4().hex
                reference_dir.mkdir(parents=True, exist_ok=True)
                uploaded = {}
                for field in ("originalSubtitle", "editedSubtitle"):
                    item = form[field] if field in form else None
                    if item is None or not getattr(item, "file", None) or not item.filename:
                        continue
                    suffix = Path(item.filename).suffix.lower()
                    if suffix not in {".srt", ".vtt", ".ass"}:
                        raise RuntimeError("ຮອງຮັບ subtitle ສຳລັບການຮຽນຮູ້: SRT, VTT ຫຼື ASS.")
                    target = reference_dir / f"{field}{suffix}"
                    with target.open("wb") as output: shutil.copyfileobj(item.file, output)
                    if target.stat().st_size > 5 * 1024 * 1024:
                        target.unlink(missing_ok=True); raise RuntimeError("subtitle reference ໃຫຍ່ເກີນ 5 MB.")
                    uploaded[field] = target
                threading.Thread(target=analyze_editing_style_job, args=(capcut_id, uploaded.get("originalSubtitle"), uploaded.get("editedSubtitle")), daemon=True).start()
                return self.json({"ok":True},202)
            except Exception as exc:
                return self.json({"error":str(exc)[:400]},400)
        if self.path == "/api/editing-style/rules":
            try:
                length = int(self.headers.get("Content-Length", 0)); payload = json.loads(self.rfile.read(length))
                profile = load_editing_profile(EDITING_STYLE_PROFILE)
                action = str(payload.get("action", "add"))
                if action == "add":
                    added = add_manual_removed_words(profile, payload.get("words", ""))
                else:
                    word = " ".join(str(payload.get("word", "")).strip().split())
                    if not word or word not in profile.get("removed_words", {}):
                        raise RuntimeError("ບໍ່ພົບ rule ທີ່ເລືອກ.")
                    if action == "toggle": profile["removed_words"][word]["enabled"] = not bool(profile["removed_words"][word].get("enabled", True))
                    elif action == "delete": del profile["removed_words"][word]
                    else: raise RuntimeError("ຄຳສັ່ງ rule ບໍ່ຖືກຕ້ອງ.")
                    added = []
                save_editing_profile(EDITING_STYLE_PROFILE, profile)
                return self.json({"ok":True, "added":added, "rules":profile.get("removed_words", {})})
            except Exception as exc:
                return self.json({"error":str(exc)[:300]},400)
        if self.path != "/api/project": return self.json({"error":"not found"},404)
        try:
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD":"POST","CONTENT_TYPE":self.headers.get("Content-Type")})
            data = json.loads(form.getfirst("project", "{}"))
        except (ValueError, TypeError, json.JSONDecodeError):
            STATUS = {"state":"error", "message":"ຂໍ້ມູນ project ບໍ່ຖືກຕ້ອງ.", "progress":0, "files":[]}
            return self.json({"error":"ຂໍ້ມູນ project ບໍ່ຖືກຕ້ອງ"},400)
        STATUS = {"state":"working", "message":"ກຳລັງບັນທຶກ project (35%)", "progress":35, "files":[]}
        project_id = clean(data.get("id") or uuid.uuid4().hex); data["id"] = project_id
        directory = WORK / project_id; directory.mkdir(exist_ok=True); files = data.setdefault("files", {})
        if project_path(project_id).exists():
            previous_project = load_project(project_id)
            files.update(previous_project.get("files", {}))
            data["captions"] = preserve_caption_metadata(previous_project.get("captions", []), data.get("captions", []))
            for field in ("transcription", "caption_history", "sfx_events", "srt_file"):
                if field in previous_project and field not in data:
                    data[field] = copy.deepcopy(previous_project[field])
        for field in ("video","sfx"):
            item = form[field] if field in form else None
            if item is not None and getattr(item,"file",None) is not None and item.filename:
                target = directory / clean(item.filename)
                with target.open("wb") as output: shutil.copyfileobj(item.file, output)
                files[field] = str(target)
        if not files.get("video"):
            STATUS = {"state":"error", "message":"ກະລຸນາເລືອກ video ກ່ອນບັນທຶກ.", "progress":0, "files":[]}
            return self.json({"error":"ຕ້ອງເລືອກ video"},400)
        data["updated"] = time.time(); save_project(data)
        STATUS = {"state":"done", "message":"ບັນທຶກ project ສຳເລັດແລ້ວ.", "progress":100, "files":[]}
        return self.json({"id":project_id,"project":data})

if __name__ == "__main__":
    os.chdir(ROOT); print(f"JodSub: http://127.0.0.1:{PORT}")
    if PACKAGED:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/")).start()
    ThreadingHTTPServer(("127.0.0.1",PORT),App).serve_forever()
