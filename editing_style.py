"""Evidence-based, non-destructive editing-style analysis for JodSub.

This module deliberately keeps reference analysis separate from the editing
engine.  A CapCut timeline is evidence of *what was kept*, but not proof that
every omitted interval is silence or an unwanted word.  The report therefore
records measured observations and only promotes rules after repeated evidence.
"""
from __future__ import annotations

import json
import math
import statistics
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path


SCHEMA_VERSION = 1
MIN_REFERENCES_FOR_AUTOMATION = 3
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".mov", ".mp4", ".mkv", ".webm"}


def _db(value: float) -> float:
    return round(20.0 * math.log10(max(float(value), 1e-12)), 2)


def _median(values):
    return round(float(statistics.median(values)), 2) if values else None


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * max(0.0, min(1.0, percentile))
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def default_profile():
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": None,
        "reference_count": 0,
        "automation_enabled": False,
        "automation_min_references": MIN_REFERENCES_FOR_AUTOMATION,
        "confidence": {"auto_cut": 0.90, "suggest": 0.65},
        "silence": {
            "threshold_db": None,
            "minimum_ms": None,
            "maximum_ms": None,
            "average_ms": None,
            "pre_roll_ms": None,
            "post_roll_ms": None,
            "fade_ms": None,
            "evidence_count": 0,
        },
        "removed_words": {},
        "removed_phrases": {},
        "references": [],
        "manual_feedback": [],
    }


def load_profile(path: Path):
    if not path.is_file():
        return default_profile()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return default_profile()
    profile = default_profile()
    for key, value in saved.items():
        if key in profile:
            profile[key] = value
    return profile


def save_profile(path: Path, profile):
    path.parent.mkdir(parents=True, exist_ok=True)
    profile["updated_at"] = time.time()
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def _media_duration(path: Path, ffmpeg: str) -> float:
    """Read duration through ffmpeg stderr; no dependency on ffprobe."""
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False,
    )
    import re
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr)
    if not match:
        raise RuntimeError(f"ອ່ານຄວາມຍາວ media ບໍ່ໄດ້: {path.name}")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _extract_audio(path: Path, ffmpeg: str):
    target = Path(tempfile.gettempdir()) / f"jodsub-style-{uuid.uuid4().hex}.wav"
    subprocess.run(
        [ffmpeg, "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(target)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
    )
    return target


def _audio_windows(audio, rate, step_ms=10):
    """Return 10ms RMS dB windows.  Analysis is in one pass over audio."""
    import numpy as np
    step = max(1, int(rate * step_ms / 1000))
    usable = len(audio) - len(audio) % step
    if usable <= 0:
        return np.array([], dtype=float)
    chunks = audio[:usable].reshape(-1, step)
    return 20 * np.log10(np.maximum(np.sqrt(np.mean(chunks * chunks, axis=1)), 1e-12))


def _range_metrics(audio, rate, start, end):
    import numpy as np
    left = max(0, int(round(float(start) * rate)))
    right = min(len(audio), int(round(float(end) * rate)))
    data = audio[left:right]
    if len(data) == 0:
        return {"rms_db": None, "peak_db": None, "lufs": None}
    return {
        "rms_db": _db(float(np.sqrt(np.mean(data * data)))),
        "peak_db": _db(float(np.max(np.abs(data)))),
        # True EBU R128 LUFS requires gated program analysis.  Do not label
        # a simple RMS approximation as LUFS.
        "lufs": None,
    }


def _merge_ranges(ranges, epsilon=0.002):
    merged = []
    for left, right in sorted((float(a), float(b)) for a, b in ranges if b > a):
        if merged and left <= merged[-1][1] + epsilon:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _resolve_media_path(path: Path, media_roots):
    if path.is_file():
        return path
    for root in media_roots or []:
        candidate = Path(root) / path.name
        if candidate.is_file():
            return candidate
    return path


def parse_capcut_reference(draft: Path, media_roots=()):
    """Extract the kept source intervals from a real CapCut timeline.

    The function never assumes that timeline ordering equals source ordering;
    editors may intentionally reorder clips.  Gaps are calculated in source
    time only, while ``reordered`` is reported so the caller can limit what it
    learns from a reference.
    """
    try:
        data = json.loads(draft.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("ອ່ານ CapCut timeline ບໍ່ໄດ້.") from exc
    materials = {item.get("id"): item for item in data.get("materials", {}).get("videos", [])}
    per_source, timeline_order = {}, []
    for track in data.get("tracks", []):
        if track.get("type") != "video":
            continue
        for segment in track.get("segments", []):
            material = materials.get(segment.get("material_id"))
            if not material:
                continue
            source_path = _resolve_media_path(Path(str(material.get("path", ""))), media_roots)
            source = segment.get("source_timerange") or {}
            target = segment.get("target_timerange") or {}
            start = float(source.get("start", 0)) / 1_000_000
            end = start + float(source.get("duration", 0)) / 1_000_000
            if not source_path.is_file() or end <= start:
                continue
            key = str(source_path.resolve())
            per_source.setdefault(key, {"path": source_path, "ranges": []})["ranges"].append((start, end))
            timeline_order.append((float(target.get("start", 0)), key, start))
    if not per_source:
        raise RuntimeError("CapCut timeline ບໍ່ພົບ source video ທີ່ອ່ານໄດ້.")
    # A reference must identify one source video.  Do not silently blend cuts
    # from different cameras/files into a single learned profile.
    key, selected = max(per_source.items(), key=lambda item: sum(end - start for start, end in item[1]["ranges"]))
    order = [source_start for _, source_key, source_start in sorted(timeline_order) if source_key == key]
    reordered = any(order[index] + .002 < order[index - 1] for index in range(1, len(order)))
    return {"source": selected["path"], "kept": _merge_ranges(selected["ranges"]), "reordered": reordered, "timeline": data}


def _rolls_from_kept(windows_db, kept, threshold_db):
    """Measure audible-room before/after words at real CapCut cut edges."""
    values_pre, values_post = [], []
    if threshold_db is None:
        return values_pre, values_post
    for left, right in kept:
        start, end = max(0, int(left * 100)), min(len(windows_db), int(right * 100))
        if end - start < 3:
            continue
        run = 0
        for value in windows_db[start:min(end, start + 40)]:
            if value < threshold_db:
                run += 1
            else:
                break
        if run:
            values_pre.append(run * 10)
        run = 0
        for value in windows_db[max(start, end - 40):end][::-1]:
            if value < threshold_db:
                run += 1
            else:
                break
        if run:
            values_post.append(run * 10)
    return values_pre, values_post


def analyze_capcut_reference(draft: Path, ffmpeg: str, original_srt=None, edited_srt=None, media_roots=()):
    """Create an auditable report from one CapCut reference, without edits."""
    import numpy as np
    import soundfile as sf

    reference = parse_capcut_reference(draft, media_roots)
    source, kept = reference["source"], reference["kept"]
    duration = _media_duration(source, ffmpeg)
    cuts = []
    cursor = 0.0
    for left, right in kept:
        if left - cursor >= .04:
            cuts.append((cursor, left))
        cursor = max(cursor, right)
    if duration - cursor >= .04:
        cuts.append((cursor, duration))
    wav = _extract_audio(source, ffmpeg)
    try:
        audio, rate = sf.read(str(wav), always_2d=False, dtype="float64")
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        audio = np.asarray(audio, dtype=float)
        windows_db = _audio_windows(audio, rate)
        floor_db = _percentile(windows_db.tolist(), .18)
        # This is a measured *candidate* activity boundary, not a hard-coded
        # global threshold.  Only quiet removed ranges promote it to a profile.
        activity_db = round(float(floor_db) + 6.0, 2) if floor_db is not None else None
        observations = []
        quiet_durations = []
        for start, end in cuts:
            metrics = _range_metrics(audio, rate, start, end)
            is_quiet = activity_db is not None and metrics["rms_db"] is not None and metrics["rms_db"] <= activity_db
            if is_quiet:
                quiet_durations.append((end - start) * 1000)
            observations.append({
                "start": round(start, 6), "end": round(end, 6),
                "duration_ms": round((end - start) * 1000, 1),
                **metrics, "classification": "quiet_candidate" if is_quiet else "edited_content_or_uncertain",
            })
        pre_roll, post_roll = _rolls_from_kept(windows_db, kept, activity_db)
    finally:
        wav.unlink(missing_ok=True)
    subtitle_comparison = compare_subtitles(original_srt, edited_srt) if original_srt and edited_srt else {"available": False, "reason": "ບໍ່ມີ subtitle ຕົ້ນສະບັບ ແລະ subtitle ຫຼັງຕັດເປັນຄູ່."}
    return {
        "id": uuid.uuid4().hex,
        "created_at": time.time(),
        "source_video": str(source), "capcut_timeline": str(draft),
        "source_duration_s": round(duration, 6), "edited_duration_s": round(sum(end - start for start, end in kept), 6),
        "kept_segments": [{"start": round(a, 6), "end": round(b, 6)} for a, b in kept],
        "removed_segments": observations,
        "timeline_reordered": reference["reordered"],
        "audio_measurement": {"noise_floor_db": floor_db, "candidate_activity_threshold_db": activity_db, "lufs": None},
        "silence_observations": {
            "count": len(quiet_durations), "minimum_ms": min(quiet_durations) if quiet_durations else None,
            "maximum_ms": max(quiet_durations) if quiet_durations else None,
            "average_ms": round(sum(quiet_durations) / len(quiet_durations), 1) if quiet_durations else None,
            "pre_roll_ms": _median(pre_roll), "post_roll_ms": _median(post_roll),
        },
        "subtitle_comparison": subtitle_comparison,
        "limitations": (["timeline ມີການຈັດລຽງ clip ໃໝ່; ຈຶ່ງບໍ່ໃຊ້ມັນເປັນຫຼັກຖານວ່າທຸກຊ່ວງທີ່ຫາຍໄປເປັນ silence."] if reference["reordered"] else []) + ([] if subtitle_comparison.get("available") else [subtitle_comparison["reason"]]),
    }


def _read_srt(path):
    import re
    if not path or not Path(path).is_file():
        return []
    subtitle = Path(path)
    content = subtitle.read_text(encoding="utf-8-sig", errors="replace")
    if subtitle.suffix.lower() == ".ass":
        rows = []
        for line in content.splitlines():
            if not line.casefold().startswith("dialogue:"):
                continue
            fields = line.split(":", 1)[1].split(",", 9)
            if len(fields) == 10:
                rows.append(fields[9].replace(r"\N", " "))
        return [re.sub(r"\{[^}]*\}", "", re.sub(r"\s+", " ", text)).strip() for text in rows if text.strip()]
    # Works for SRT and WebVTT, including VTT cue settings after the end time.
    pattern = r"(?:^|\n)\s*(?:\d+\s*\n)?\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->[^\n]*\n(.*?)(?=\n\s*\n|\Z)"
    return [re.sub(r"<[^>]+>", "", re.sub(r"\s+", " ", text)).strip() for text in re.findall(pattern, content, re.S) if text.strip()]


def compare_subtitles(original_srt, edited_srt):
    """Conservative cue comparison; Lao without word separators is not split."""
    from difflib import SequenceMatcher
    original, edited = _read_srt(original_srt), _read_srt(edited_srt)
    if not original or not edited:
        return {"available": False, "reason": "subtitle ຄູ່ໜຶ່ງວ່າງ ຫຼືອ່ານບໍ່ໄດ້."}
    # A token is only learned when explicitly separated.  This avoids
    # inventing Lao word boundaries from a sentence-level SRT.
    original_tokens = " ".join(original).split()
    edited_tokens = " ".join(edited).split()
    removed = []
    for tag, a1, a2, _, _ in SequenceMatcher(a=original_tokens, b=edited_tokens, autojunk=False).get_opcodes():
        if tag in {"delete", "replace"}:
            removed.extend(original_tokens[a1:a2])
    counts, found = Counter(removed), Counter(original_tokens)
    return {"available": True, "method": "whitespace_token_comparison", "removed_words": [{"word": key, "found": found[key], "removed": value} for key, value in counts.most_common()], "warning": "ການຮຽນຄຳພາສາລາວຕ້ອງໃຊ້ word timestamps ຈາກ JodSub/forced alignment; SRT ທີ່ບໍ່ແບ່ງຄຳຈະບໍ່ຖືກ blacklist."}


def update_profile(profile, report):
    """Aggregate only measured quiet cuts and explicitly compared words."""
    # Re-analysing the same timeline replaces its prior observation instead
    # of inflating confidence as though it were a new editing decision.
    references = [item for item in profile.get("references", []) if item.get("capcut_timeline") != report.get("capcut_timeline")]
    references.append(report)
    profile["references"] = references[-50:]
    profile["reference_count"] = len(profile["references"])
    quiet = [item for ref in profile["references"] for item in ref.get("removed_segments", []) if item.get("classification") == "quiet_candidate"]
    silence = profile["silence"]
    silence["evidence_count"] = len(quiet)
    if quiet:
        duration = [float(item["duration_ms"]) for item in quiet]
        threshold = [float(item["rms_db"]) for item in quiet if item.get("rms_db") is not None]
        silence.update({"threshold_db": round(_percentile(threshold, .85) + 1.5, 2) if threshold else None, "minimum_ms": round(min(duration), 1), "maximum_ms": round(max(duration), 1), "average_ms": round(sum(duration) / len(duration), 1)})
    pre = [ref.get("silence_observations", {}).get("pre_roll_ms") for ref in profile["references"]]
    post = [ref.get("silence_observations", {}).get("post_roll_ms") for ref in profile["references"]]
    silence["pre_roll_ms"] = _median([value for value in pre if value is not None])
    silence["post_roll_ms"] = _median([value for value in post if value is not None])
    previous_words = profile.get("removed_words", {})
    word_stats = {}
    for ref in profile["references"]:
        for item in ref.get("subtitle_comparison", {}).get("removed_words", []):
            word = item["word"]
            if not word:
                continue
            previous = previous_words.get(word, {})
            row = word_stats.setdefault(word, {"found": 0, "removed": 0, "enabled": previous.get("enabled", True), "manual_keep": previous.get("manual_keep", 0)})
            row["found"] += int(item.get("found", 0)); row["removed"] += int(item.get("removed", 0))
            row["confidence"] = round(row["removed"] / max(1, row["found"] + row.get("manual_keep", 0)), 3)
    profile["removed_words"] = word_stats
    profile["automation_enabled"] = profile["reference_count"] >= int(profile.get("automation_min_references", MIN_REFERENCES_FOR_AUTOMATION)) and silence.get("evidence_count", 0) >= 3
    return profile


def add_manual_removed_words(profile, values):
    """Persist explicit user choices without fabricating statistical evidence."""
    if isinstance(values, str):
        values = values.replace(",", "\n").splitlines()
    cleaned = []
    for value in values or []:
        word = " ".join(str(value).strip().split())
        if not word or len(word) > 120 or any(ord(char) < 32 for char in word):
            continue
        if word not in cleaned:
            cleaned.append(word)
    if not cleaned:
        raise ValueError("ບໍ່ພົບຄຳ ຫຼື ວະລີທີ່ບັນທຶກໄດ້.")
    rules = profile.setdefault("removed_words", {})
    for word in cleaned:
        previous = rules.get(word, {})
        rules[word] = {
            "found": int(previous.get("found", 0)),
            "removed": int(previous.get("removed", 0)),
            "enabled": True,
            "manual_keep": int(previous.get("manual_keep", 0)),
            "confidence": 1.0,
            "source": "manual",
        }
    return cleaned
