#!/usr/bin/env python3
"""
make_sample_data.py - write fake Yogaboo recordings in the exact file formats the
phone app produces, so you can test the uploader / portal without real subjects.

    python make_sample_data.py ./sample_YogabooApp
    python yogaboo_upload.py ./sample_YogabooApp --dry-run
"""
import math, random, struct, sys, time, wave
from pathlib import Path

SCREEN_W, SCREEN_H = 1080, 2173


def write_gyro(path, t0, secs=190, hz=50, calm=1.0):
    with open(path, "w") as f:
        f.write("Timestamp,Acc_X,Acc_Y,Acc_Z,Gyro_X,Gyro_Y,Gyro_Z,Wobble_Index\n")
        wob = 0.0
        for i in range(secs * hz):
            t = i / hz
            decay = math.exp(-t / 6)  # initial pick-up spike, then settles
            ax = 0.3 * decay * math.sin(t * 5) + random.gauss(0, 0.03 * calm)
            ay = -2.5 * decay * math.sin(t * 3) + random.gauss(0, 0.03 * calm)
            az = 2.4 * decay * math.sin(t * 4) + random.gauss(0, 0.04 * calm)
            gx = 3.0 * decay * math.sin(t * 2) + random.gauss(0, 0.02 * calm)
            gy = 0.4 * decay + random.gauss(0, 0.02 * calm)
            gz = 0.7 * decay * math.cos(t * 3) + random.gauss(0, 0.02 * calm)
            wob = 0.98 * wob + 0.02 * (abs(ax) + abs(ay) + abs(az) + abs(gx) + abs(gy) + abs(gz))
            f.write("%d,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.4f\n"
                    % (t0 + int(t * 1000), ax, ay, az, gx, gy, gz, wob))


def write_gaze(path, t0, n=600, spread=250, drift=True):
    with open(path, "w") as f:
        f.write("timestamp,left_gaze_x,left_gaze_y,right_gaze_x,right_gaze_y,avg_gaze_x,avg_gaze_y\n")
        x, y = SCREEN_W * 0.4, SCREEN_H * 0.3
        for i in range(n):
            if drift:
                x += random.gauss(0, 35); y += random.gauss(8, 40)
            else:
                x = SCREEN_W * 0.45 + random.gauss(0, spread * 0.4)
                y = SCREEN_H * 0.35 + random.gauss(0, spread * 0.5)
            x = min(max(x, 0), SCREEN_W); y = min(max(y, 0), SCREEN_H)
            lx, ly = x + random.gauss(0, 12), y + random.gauss(0, 12)
            rx, ry = x + random.gauss(0, 12), y + random.gauss(0, 12)
            f.write("%d,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f\n"
                    % (t0 + i * 33, lx, ly, rx, ry, (lx + rx) / 2, (ly + ry) / 2))


def write_balance(path, t0, secs=30, fps=30, sway=0.01):
    with open(path, "w") as f:
        f.write("timestamp,nose_x,nose_y,left_shoulder_x,left_shoulder_y,right_shoulder_x,"
                "right_shoulder_y,left_hip_x,left_hip_y,right_hip_x,right_hip_y\n")
        cx, cy = 0.5, 0.5
        for i in range(secs * fps):
            cx += random.gauss(0, sway) - (cx - 0.5) * 0.08
            cy += random.gauss(0, sway * 0.6) - (cy - 0.5) * 0.08
            row = [t0 + i * 33,
                   cx, cy - 0.30, cx - 0.08, cy - 0.18, cx + 0.08, cy - 0.18,
                   cx - 0.05, cy + 0.08, cx + 0.05, cy + 0.08]
            f.write(",".join(["%d" % row[0]] + ["%.4f" % v for v in row[1:]]) + "\n")


def write_voice(path, secs=9, rate=44100):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        frames = bytearray()
        for i in range(secs * rate):
            t = i / rate
            env = 0.28 * (1 - t / (secs * 1.1)) * (0.7 + 0.3 * math.sin(t * 2.1))
            s = env * math.sin(2 * math.pi * 220 * t) * random.uniform(0.4, 1.0)
            frames += struct.pack("<h", int(max(-1, min(1, s)) * 32767))
        w.writeframes(bytes(frames))


def main(out):
    random.seed(7)
    out = Path(out)
    base = int(time.time() * 1000) - 3 * 24 * 3600 * 1000
    for code, calm, spread in [("Subject1", 1.0, 250), ("Subject2", 1.8, 400)]:
        d = out / f"Participant_{code}"
        d.mkdir(parents=True, exist_ok=True)
        write_gyro(d / "gyro_acc_data.csv", base, calm=calm)
        write_gaze(d / "iris_data.csv", base + 300_000)
        write_gaze(d / "fixation_data.csv", base + 400_000, n=450, spread=spread, drift=False)
        write_balance(d / "MediapipeBalance_data.csv", base + 500_000, sway=0.008 * calm)
        write_voice(d / "voice_test.wav")
    print("Wrote sample data to", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "./sample_YogabooApp")
