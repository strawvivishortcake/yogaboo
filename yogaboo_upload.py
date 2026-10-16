#!/usr/bin/env python3
"""
yogaboo_upload.py - push Yogaboo phone recordings to Supabase.

The Yogaboo Android app writes each participant's recordings to
    <phone>/Download/YogabooApp/Participant_<id>/
        gyro_acc_data.csv         accelerometer + gyroscope  (Screen2)
        iris_data.csv             gaze tracking              (GazeTestActivity)
        fixation_data.csv         gaze fixation              (FixationTestActivity)
        MediapipeBalance_data.csv pose landmarks for balance (Screen2verStill)
        voice_test.wav            voice amplitude            (Screen3)

Copy that folder to a computer (USB / `adb pull`, see README), then:

    python yogaboo_upload.py ./YogabooApp --session baseline
    python yogaboo_upload.py ./YogabooApp/Participant_Subject1 --session middle --age 11
    python yogaboo_upload.py ./YogabooApp --dry-run          # parse only, no network

For every participant folder it
  1. parses each recording and computes summary metrics (for the portal tables/trends),
  2. uploads the raw file to the private `yogaboo` Storage bucket,
  3. upserts rows into yogaboo_participants / yogaboo_sessions / yogaboo_tests.
Re-running with the same participant + session label replaces that session's data.

Only dependency: `requests` (pip install requests). See .env.example for settings.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

BUCKET = "yogaboo"

# test_type -> file the app writes
TEST_FILES = {
    "gyro_acc": "gyro_acc_data.csv",
    "gaze_tracking": "iris_data.csv",
    "fixation": "fixation_data.csv",
    "balance": "MediapipeBalance_data.csv",
    "voice": "voice_test.wav",
}

# The phone's gaze charts hide points further than this many standard deviations
# from the mean (GazeChartView.outlierStdDevThreshold = 1.8f in the APK).
OUTLIER_STD = 1.8
# Balance landmarks outside this normalised range are discarded by the phone's
# balance graph (balancemediapipegraph.loadAndPlot).
BALANCE_MIN, BALANCE_MAX = -0.5, 1.5


# ----------------------------------------------------------------- helpers

def _f(s: Any) -> Optional[float]:
    try:
        v = float(str(s).strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _r(v: Optional[float], nd: int = 4) -> Optional[float]:
    return None if v is None else round(v, nd)


def _mean(xs: list[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _std(xs: list[float]) -> Optional[float]:
    if len(xs) < 2:
        return 0.0 if xs else None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _rms(xs: list[float]) -> Optional[float]:
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else None


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """Header (lower-cased) + rows. Rows with the wrong column count (e.g. a line cut
    off when the app was closed mid-write) are skipped."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            return [], []
        rows = [r for r in reader if len(r) == len(header)]
    return header, rows


def col_index(header: list[str], name: str, fallback: int) -> int:
    return header.index(name) if name in header else fallback


def ts_span(ts: list[float]) -> tuple[Optional[float], Optional[float]]:
    """(first timestamp ms, duration seconds). Timestamps are epoch milliseconds."""
    if not ts:
        return None, None
    return min(ts), (max(ts) - min(ts)) / 1000.0


def plausible_epoch_ms(v: Optional[float]) -> bool:
    return v is not None and 1.5e12 < v < 4.1e12  # 2017 .. 2099


# --------------------------------------------------------------- summaries

def summarize_gyro_acc(path: Path) -> dict:
    header, rows = read_csv(path)
    ix = {k: col_index(header, k, i) for i, k in enumerate(
        ["timestamp", "acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z", "wobble_index"])}
    cols = {k: [] for k in ix}
    for r in rows:
        vals = {k: _f(r[i]) for k, i in ix.items()}
        if vals["timestamp"] is None:
            continue
        for k, v in vals.items():
            if v is not None:
                cols[k].append(v)
    ts = cols["timestamp"]
    start, dur = ts_span(ts)
    summ: dict[str, Any] = {}
    if dur:
        summ["sample_rate_hz"] = _r(len(ts) / dur, 2)
    for k in ["acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z"]:
        summ[f"{k}_rms"] = _r(_rms(cols[k]))
    w = cols["wobble_index"]
    if w:
        summ.update(wobble_mean=_r(_mean(w)), wobble_max=_r(max(w)), wobble_last=_r(w[-1]))
    return {"n": len(ts), "start_ms": start, "duration_s": dur, "summary": summ}


def gaze_points(path: Path) -> tuple[list[tuple[float, float, float]], int]:
    """[(t_ms, x_px, y_px)] from avg_gaze_x / avg_gaze_y, plus raw row count."""
    header, rows = read_csv(path)
    it = col_index(header, "timestamp", 0)
    ix = col_index(header, "avg_gaze_x", 5)
    iy = col_index(header, "avg_gaze_y", 6)
    pts = []
    for r in rows:
        t, x, y = _f(r[it]), _f(r[ix]), _f(r[iy])
        if t is not None and x is not None and y is not None:
            pts.append((t, x, y))
    return pts, len(rows)


def split_outliers(pts: list[tuple[float, float, float]], k: float = OUTLIER_STD):
    """Per-axis z-score filter at k standard deviations (approximates the phone)."""
    if len(pts) < 3:
        return pts, []
    xs, ys = [p[1] for p in pts], [p[2] for p in pts]
    mx, my, sx, sy = _mean(xs), _mean(ys), _std(xs), _std(ys)
    kept, out = [], []
    for p in pts:
        bad = (sx and abs(p[1] - mx) > k * sx) or (sy and abs(p[2] - my) > k * sy)
        (out if bad else kept).append(p)
    return kept, out


def summarize_gaze(path: Path) -> dict:
    pts, _ = gaze_points(path)
    start, dur = ts_span([p[0] for p in pts])
    kept, out = split_outliers(pts)
    summ: dict[str, Any] = {"n_points": len(pts), "n_outliers": len(out), "outlier_std": OUTLIER_STD}
    if kept:
        cx, cy = _mean([p[1] for p in kept]), _mean([p[2] for p in kept])
        dists = [math.hypot(p[1] - cx, p[2] - cy) for p in kept]
        path_len = sum(math.hypot(kept[i][1] - kept[i - 1][1], kept[i][2] - kept[i - 1][2])
                       for i in range(1, len(kept)))
        summ.update(
            centroid_x=_r(cx, 1), centroid_y=_r(cy, 1),
            mean_dist_to_centroid_px=_r(_mean(dists), 1),
            std_x=_r(_std([p[1] for p in kept]), 1), std_y=_r(_std([p[2] for p in kept]), 1),
            path_length_px=_r(path_len, 1),
        )
    return {"n": len(pts), "start_ms": start, "duration_s": dur, "summary": summ}


def balance_cop(path: Path) -> list[tuple[float, float, float]]:
    """Centre of pressure per frame, exactly as the phone computes it:
    mean of nose, shoulder-midpoint and hip-midpoint (per axis)."""
    header, rows = read_csv(path)
    idx = {n: col_index(header, n, i) for i, n in enumerate(
        ["timestamp", "nose_x", "nose_y", "left_shoulder_x", "left_shoulder_y",
         "right_shoulder_x", "right_shoulder_y", "left_hip_x", "left_hip_y",
         "right_hip_x", "right_hip_y"])}
    out = []
    for r in rows:
        v = {n: _f(r[i]) for n, i in idx.items()}
        if any(x is None for x in v.values()):
            continue
        cx = (v["nose_x"] + (v["left_shoulder_x"] + v["right_shoulder_x"]) / 2
              + (v["left_hip_x"] + v["right_hip_x"]) / 2) / 3
        cy = (v["nose_y"] + (v["left_shoulder_y"] + v["right_shoulder_y"]) / 2
              + (v["left_hip_y"] + v["right_hip_y"]) / 2) / 3
        if BALANCE_MIN <= cx <= BALANCE_MAX and BALANCE_MIN <= cy <= BALANCE_MAX:
            out.append((v["timestamp"], cx, cy))
    return out


def summarize_balance(path: Path) -> dict:
    cop = balance_cop(path)
    start, dur = ts_span([p[0] for p in cop])
    summ: dict[str, Any] = {"n_frames": len(cop)}
    if cop:
        xs, ys = [p[1] for p in cop], [p[2] for p in cop]
        path_len = sum(math.hypot(cop[i][1] - cop[i - 1][1], cop[i][2] - cop[i - 1][2])
                       for i in range(1, len(cop)))
        summ.update(
            cop_mean_x=_r(_mean(xs)), cop_mean_y=_r(_mean(ys)),
            cop_sd_x=_r(_std(xs)), cop_sd_y=_r(_std(ys)),
            cop_range_x=_r(max(xs) - min(xs)), cop_range_y=_r(max(ys) - min(ys)),
            cop_path_length=_r(path_len),
        )
    return {"n": len(cop), "start_ms": start, "duration_s": dur, "summary": summ}


def summarize_voice(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError) as e:
        return {"n": None, "start_ms": None, "duration_s": None,
                "summary": {"error": f"could not read WAV: {e}"}}
    summ: dict[str, Any] = {"sample_rate_hz": rate, "channels": ch, "bits": width * 8}
    if width == 2 and raw:
        import array
        a = array.array("h")
        a.frombytes(raw[: len(raw) // 2 * 2])
        if sys.byteorder == "big":
            a.byteswap()
        step = ch  # first channel only
        vals = [x / 32768.0 for x in a[::step]]
        summ.update(peak=_r(max(abs(x) for x in vals)), rms=_r(_rms(vals)))
        win = max(1, int(rate * 0.1))  # 100 ms windows
        env = [_rms(vals[i:i + win]) for i in range(0, len(vals), win)]
        env = [e for e in env if e is not None]
        if env:
            summ.update(envelope_mean=_r(_mean(env)), envelope_std=_r(_std(env)))
    return {"n": n, "start_ms": None, "duration_s": (n / rate) if rate else None, "summary": summ}


SUMMARIZERS = {
    "gyro_acc": summarize_gyro_acc,
    "gaze_tracking": summarize_gaze,
    "fixation": summarize_gaze,
    "balance": summarize_balance,
    "voice": summarize_voice,
}


# ------------------------------------------------------------ discovery

def find_participant_dirs(root: Path) -> list[Path]:
    if re.match(r"(?i)^participant_", root.name):
        return [root]
    return sorted(p for p in root.rglob("*") if p.is_dir() and re.match(r"(?i)^participant_", p.name))


def participant_code(folder: Path) -> str:
    return re.sub(r"(?i)^participant_", "", folder.name) or folder.name


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iso_from_ms(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def scan_participant(folder: Path) -> dict:
    tests = {}
    for ttype, fname in TEST_FILES.items():
        p = folder / fname
        if not p.is_file() or p.stat().st_size == 0:
            continue
        parsed = SUMMARIZERS[ttype](p)
        parsed.update(path=p, file_name=fname, size=p.stat().st_size)
        tests[ttype] = parsed
    starts = [t["start_ms"] for t in tests.values() if plausible_epoch_ms(t.get("start_ms"))]
    if starts:
        recorded_ms = min(starts)
    elif tests:  # fall back to file modification time
        recorded_ms = min(t["path"].stat().st_mtime for t in tests.values()) * 1000
    else:
        recorded_ms = None
    return {"folder": folder, "code": participant_code(folder), "tests": tests,
            "recorded_ms": recorded_ms}


# ------------------------------------------------------------ Supabase REST

class Supabase:
    def __init__(self, url: str, apikey: str):
        import requests  # imported lazily so --dry-run works without it
        self.http = requests.Session()
        self.url = url.rstrip("/")
        self.apikey = apikey
        self.token = apikey  # replaced by the user's JWT after sign_in()

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"apikey": self.apikey, "Authorization": f"Bearer {self.token}"}
        h.update(extra or {})
        return h

    def _check(self, resp, what: str):
        if resp.status_code >= 300:
            hint = ""
            if resp.status_code in (401, 403):
                hint = ("\n  -> Permission denied. Sign in as a Researcher/Administrator "
                        "and make sure sql/yogaboo_schema.sql has been run.")
            raise SystemExit(f"{what} failed: HTTP {resp.status_code} {resp.text[:400]}{hint}")
        return resp

    def sign_in(self, username: str, password: str) -> None:
        email = username if "@" in username else f"{username.strip().lower()}@fakewebsite.com"
        r = self.http.post(f"{self.url}/auth/v1/token?grant_type=password",
                           headers={"apikey": self.apikey, "Content-Type": "application/json"},
                           json={"email": email, "password": password}, timeout=30)
        self._check(r, "Sign-in")
        self.token = r.json()["access_token"]

    def upsert(self, table: str, row: dict, on_conflict: str) -> dict:
        r = self.http.post(
            f"{self.url}/rest/v1/{table}?on_conflict={on_conflict}",
            headers=self._headers({"Content-Type": "application/json",
                                   "Prefer": "resolution=merge-duplicates,return=representation"}),
            data=json.dumps(row), timeout=60)
        self._check(r, f"Upsert into {table}")
        return r.json()[0]

    def upload_file(self, object_path: str, path: Path) -> None:
        ctype = "audio/wav" if path.suffix.lower() == ".wav" else "text/csv"
        safe = "/".join(quote(seg, safe="") for seg in object_path.split("/"))
        with open(path, "rb") as fh:
            r = self.http.post(f"{self.url}/storage/v1/object/{BUCKET}/{safe}",
                               headers=self._headers({"Content-Type": ctype, "x-upsert": "true"}),
                               data=fh, timeout=300)
        self._check(r, f"Upload of {path.name}")


# ------------------------------------------------------------------- main

def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Upload Yogaboo recordings to Supabase.")
    ap.add_argument("path", type=Path, help="A Participant_<id> folder, or a folder containing several "
                                            "(e.g. the YogabooApp folder pulled from the phone).")
    ap.add_argument("--session", help="Session label, e.g. baseline / middle / end. "
                                      "Default: session-YYYY-MM-DD from the recording date.")
    ap.add_argument("--age", type=int, help="Participant age in years (only used when creating/updating).")
    ap.add_argument("--portal-user-id", help="Link the participant to a portal account (auth.users uuid) "
                                             "so that user / their provider can view the data.")
    ap.add_argument("--screen", help="Phone screen size in px, e.g. 1080x2173 (scales the gaze charts).")
    ap.add_argument("--device-model", help="Phone model, e.g. 'Samsung Galaxy S21'.")
    ap.add_argument("--app-version", default=None, help="Yogaboo app version (default: unknown).")
    ap.add_argument("--calibration-json", type=Path, help="Optional JSON with the phone's calibration "
                    "values (cal_ax, cal_bx, cal_cx, cal_ay, cal_by, cal_cy).")
    ap.add_argument("--pass-threshold", type=float, help="Pass threshold set in the app, if known.")
    ap.add_argument("--notes", help="Free-text note stored on the session.")
    ap.add_argument("--dry-run", action="store_true", help="Parse and print only; no network access.")
    ap.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    return ap.parse_args(argv)


def print_scan(scan: dict) -> None:
    when = iso_from_ms(scan["recorded_ms"]) if scan["recorded_ms"] else "unknown"
    print(f"\nParticipant {scan['code']}  ({scan['folder']})  recorded {when}")
    if not scan["tests"]:
        print("  (no Yogaboo files found)")
    for ttype, t in scan["tests"].items():
        dur = f"{t['duration_s']:.1f}s" if t["duration_s"] else "?"
        print(f"  {ttype:<14} {t['file_name']:<26} n={t['n']} dur={dur} {json.dumps(t['summary'])[:140]}")


def main(argv=None) -> int:
    args = parse_args(argv)
    load_env_file(args.env_file)
    if not args.path.exists():
        raise SystemExit(f"Path not found: {args.path}")

    folders = find_participant_dirs(args.path)
    if not folders:
        raise SystemExit(f"No Participant_* folders found under {args.path}")
    scans = [scan_participant(f) for f in folders]
    for s in scans:
        print_scan(s)
    scans = [s for s in scans if s["tests"]]
    if (args.age is not None or args.portal_user_id) and len(scans) > 1:
        raise SystemExit("--age and --portal-user-id describe ONE participant, but %d were found. "
                         "Point at a single Participant_<id> folder when using them." % len(scans))
    if args.dry_run:
        print("\nDry run - nothing uploaded.")
        return 0
    if not scans:
        raise SystemExit("Nothing to upload.")

    url = os.environ.get("SUPABASE_URL")
    anon = os.environ.get("SUPABASE_ANON_KEY")
    service = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not (anon or service):
        raise SystemExit("Set SUPABASE_URL and SUPABASE_ANON_KEY (or SUPABASE_SERVICE_ROLE_KEY) "
                         "- see .env.example")
    sb = Supabase(url, service or anon)
    if not service:
        user, pw = os.environ.get("YOGABOO_USER"), os.environ.get("YOGABOO_PASSWORD")
        if not user or not pw:
            raise SystemExit("Set YOGABOO_USER and YOGABOO_PASSWORD (a Researcher/Administrator "
                             "portal login) - see .env.example")
        sb.sign_in(user, pw)

    screen_w = screen_h = None
    if args.screen:
        m = re.match(r"^(\d+)\s*[xX]\s*(\d+)$", args.screen.strip())
        if not m:
            raise SystemExit("--screen must look like 1080x2173")
        screen_w, screen_h = int(m.group(1)), int(m.group(2))
    calibration = json.loads(args.calibration_json.read_text()) if args.calibration_json else None

    for s in scans:
        label = args.session or (
            "session-" + datetime.fromtimestamp(s["recorded_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if s["recorded_ms"] else "session-unknown")
        now = datetime.now(timezone.utc).isoformat()

        p_row = {"participant_code": s["code"], "updated_at": now}
        if args.age is not None:
            p_row["age_years"] = args.age
        if args.portal_user_id:
            p_row["user_id"] = args.portal_user_id
        participant = sb.upsert("yogaboo_participants", p_row, "participant_code")

        sess_row = {"participant_id": participant["id"], "session_label": label,
                    "recorded_at": iso_from_ms(s["recorded_ms"]) if s["recorded_ms"] else now,
                    "updated_at": now}
        for k, v in [("app_version", args.app_version), ("device_model", args.device_model),
                     ("screen_width", screen_w), ("screen_height", screen_h),
                     ("calibration", calibration), ("pass_threshold", args.pass_threshold),
                     ("notes", args.notes)]:
            if v is not None:
                sess_row[k] = v
        session = sb.upsert("yogaboo_sessions", sess_row, "participant_id,session_label")

        print(f"\n{s['code']} / {label}: uploading {len(s['tests'])} file(s)")
        for ttype, t in s["tests"].items():
            object_path = f"{participant['id']}/{session['id']}/{t['file_name']}"
            sb.upload_file(object_path, t["path"])
            sb.upsert("yogaboo_tests", {
                "session_id": session["id"], "test_type": ttype, "file_name": t["file_name"],
                "object_path": object_path, "file_size": t["size"], "sha256": sha256_of(t["path"]),
                "n_samples": t["n"], "duration_s": _r(t["duration_s"], 2),
                "started_at": iso_from_ms(t["start_ms"]) if plausible_epoch_ms(t.get("start_ms")) else None,
                "summary": t["summary"], "updated_at": now,
            }, "session_id,test_type")
            print(f"  ok  {ttype:<14} {t['file_name']}")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
