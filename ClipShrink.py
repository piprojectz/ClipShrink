"""
ClipShrink - pick sections of a video, join them in any order, and squeeze the result under a
size limit (e.g. Discord's 10 MB).

Requires ffmpeg + ffprobe on PATH  (winget install Gyan.FFmpeg)
Run:      python ClipShrink.py            (or drop a video onto the .py / .exe)
Optional: pip install tkinterdnd2         (enables drag-and-drop onto the drop zone)
Build exe: pip install pyinstaller && pyinstaller --onefile --noconsole ClipShrink.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # hide ffmpeg consoles on Windows


# ============================================================================ helpers

def fmt(t):
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


def parse_time(s):
    """Accepts '75', '75.5', '1:15', '1:15.5', '0:01:15'."""
    s = s.strip()
    total = 0.0
    for part in s.split(":"):
        total = total * 60 + float(part)
    return total


def probe(path):
    """Return dict with duration, width, height, fps, has_audio, size_bytes."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_type,width,height,avg_frame_rate", "-of", "json", path],
        capture_output=True, text=True, creationflags=NO_WINDOW, check=True,
    ).stdout
    info = json.loads(out)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    fps = 0.0
    try:
        n, d = v.get("avg_frame_rate", "0/1").split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    except (ValueError, ZeroDivisionError):
        pass
    return {
        "duration": float(info["format"]["duration"]),
        "size": int(info["format"].get("size", os.path.getsize(path))),
        "width": v.get("width") or 0,
        "height": v.get("height") or 1080,
        "fps": fps,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def pick_height(src_h, video_kbps, mode):
    """Auto-downscale when the bitrate budget is too thin for the source resolution."""
    if mode == "Original":
        return src_h
    if mode != "Auto":
        return min(int(mode.rstrip("p")), src_h)
    cap = 480 if video_kbps < 700 else 720 if video_kbps < 1800 else 1080 if video_kbps < 4000 else src_h
    return min(cap, src_h)


# ============================================================================ encoding core

def run_ffmpeg(args, total_dur, on_progress, lo, hi):
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y"] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW,
    )
    err_lines = []
    t_err = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
    t_err.start()
    for line in proc.stdout:
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                t = int(line.split("=")[1]) / 1_000_000
                on_progress(lo + (hi - lo) * min(max(t, 0) / total_dur, 1.0))
            except ValueError:
                pass
    rc = proc.wait()
    t_err.join(timeout=2)
    if rc != 0:
        raise RuntimeError("".join(err_lines).strip() or "ffmpeg failed")


def build_args(src, segments, height, src_h, with_audio):
    """One input per section (fast seek, any order), then concat."""
    inputs, graph, pads = [], [], []
    for i, (a, b) in enumerate(segments):
        inputs += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", src]
        graph.append(f"[{i}:v:0]setpts=PTS-STARTPTS[v{i}]")
        pads.append(f"[v{i}]")
        if with_audio:
            graph.append(f"[{i}:a:0]aresample=async=1,asetpts=PTS-STARTPTS[a{i}]")
            pads.append(f"[a{i}]")
    n = len(segments)
    graph.append(f"{''.join(pads)}concat=n={n}:v=1:a={1 if with_audio else 0}[vc]" + ("[ac]" if with_audio else ""))
    scale = f"scale=-2:{height}," if height < src_h else ""
    graph.append(f"[vc]{scale}format=yuv420p[vout]")
    maps = ["-map", "[vout]"] + (["-map", "[ac]"] if with_audio else [])
    return inputs + ["-filter_complex", ";".join(graph)] + maps


def shrink(src, dst, segments, max_mb, res_mode="Auto",
           on_progress=lambda f: None, on_status=lambda s: None):
    info = probe(src)
    dur, src_h, has_audio = info["duration"], info["height"], info["has_audio"]

    segs = []
    for a, b in segments:
        a, b = max(0.0, a), min(dur, b)
        if b - a < 0.1:
            raise ValueError(f"Section {fmt(a)}–{fmt(b)} is empty or outside the video ({fmt(dur)}).")
        segs.append((a, b))
    if not segs:
        raise ValueError("Add at least one section.")
    total = sum(b - a for a, b in segs)

    budget_kbit = max_mb * 8000   # decimal MB -> kilobits (safe for either MB definition)
    safety = 0.94
    passlog = os.path.join(tempfile.gettempdir(), "clipshrink_pass")

    for attempt in range(3):
        total_kbps = budget_kbit * safety / total
        audio_kbps = 0 if not has_audio else (128 if total_kbps > 1000 else 96 if total_kbps > 400 else 64)
        video_kbps = int(total_kbps - audio_kbps)
        if video_kbps < 100:
            raise ValueError(f"{total:.0f}s is too long to fit in {max_mb:g} MB at watchable quality.")

        h = pick_height(src_h, video_kbps, res_mode)
        on_status(f"Encoding {len(segs)} section(s), {total:.1f}s at {video_kbps} kbps, {h}p (try {attempt + 1})")
        enc = ["-c:v", "libx264", "-preset", "medium", "-b:v", f"{video_kbps}k", "-passlogfile", passlog]

        run_ffmpeg(build_args(src, segs, h, src_h, False) + enc + ["-pass", "1", "-f", "null", os.devnull],
                   total, on_progress, 0.0, 0.5)
        audio = ["-c:a", "aac", "-b:a", f"{audio_kbps}k"] if has_audio else []
        run_ffmpeg(build_args(src, segs, h, src_h, has_audio) + enc + ["-pass", "2", *audio,
                   "-movflags", "+faststart", dst], total, on_progress, 0.5, 1.0)

        size_mb = os.path.getsize(dst) / 1_000_000
        if size_mb <= max_mb:
            break
        safety *= 0.88  # overshot - squeeze harder and retry
    else:
        raise RuntimeError(f"Couldn't get under {max_mb:g} MB (last try {size_mb:.2f} MB).")

    for ext in ("-0.log", "-0.log.mbtree"):
        try:
            os.remove(passlog + ext)
        except OSError:
            pass
    return size_mb, total, h


def grab_frame(src, t, out_png, width=320):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1",
                    "-vf", f"scale={width}:-2", out_png],
                   capture_output=True, creationflags=NO_WINDOW, check=True)


def default_output(src):
    base, _ = os.path.splitext(src)
    return base + "_discord.mp4"


# ============================================================================ GUI

def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        root = TkinterDnD.Tk()
        dnd = True
    except ImportError:
        root = tk.Tk()
        dnd = False

    root.title("ClipShrink")
    root.minsize(860, 420)
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    BG = style.lookup("TFrame", "background") or root.cget("bg")
    ACCENT, TRACK, HANDLE = "#5865F2", "#c9ccd1", "#2b2d31"  # Discord blurple

    state = {"src": None, "info": None, "out": None, "preview_job": 0}
    preview_png = os.path.join(tempfile.gettempdir(), "clipshrink_preview.png")

    # ------------------------------------------------------------ range slider widget
    class RangeSlider(tk.Canvas):
        PAD, H, R = 10, 30, 7

        def __init__(self, master, on_change, on_release, width=440):
            super().__init__(master, width=width, height=self.H, bg=BG, highlightthickness=0, cursor="hand2")
            self.w, self.dur, self.a, self.b = width, 1.0, 0.0, 1.0
            self.on_change, self.on_release, self.drag = on_change, on_release, None
            self.bind("<Button-1>", self._press)
            self.bind("<B1-Motion>", self._move)
            self.bind("<ButtonRelease-1>", self._up)
            self.bind("<Configure>", lambda e: (setattr(self, "w", e.width), self.redraw()))

        def x(self, t):
            return self.PAD + (self.w - 2 * self.PAD) * (t / self.dur if self.dur else 0)

        def t(self, x):
            return min(max((x - self.PAD) / (self.w - 2 * self.PAD) * self.dur, 0), self.dur)

        def set(self, a, b, dur=None):
            if dur is not None:
                self.dur = max(dur, 0.1)
            self.a, self.b = a, b
            self.redraw()

        def redraw(self):
            self.delete("all")
            y = self.H // 2
            self.create_line(self.PAD, y, self.w - self.PAD, y, fill=TRACK, width=6, capstyle="round")
            self.create_line(self.x(self.a), y, self.x(self.b), y, fill=ACCENT, width=6)
            for t in (self.a, self.b):
                cx = self.x(t)
                self.create_oval(cx - self.R, y - self.R, cx + self.R, y + self.R, fill="white",
                                 outline=HANDLE, width=2)

        def _press(self, e):
            da, db = abs(e.x - self.x(self.a)), abs(e.x - self.x(self.b))
            self.drag = "a" if (da < db or (da == db and e.x < self.x(self.a))) else "b"
            self._move(e)

        def _move(self, e):
            if not self.drag:
                return
            t = self.t(e.x)
            if self.drag == "a":
                self.a = min(t, self.b - 0.1)
            else:
                self.b = max(t, self.a + 0.1)
            self.redraw()
            self.on_change(self.a, self.b)

        def _up(self, e):
            if self.drag:
                self.on_release(self.a if self.drag == "a" else self.b)
            self.drag = None

    # ------------------------------------------------------------ section row
    sections = []

    class Section:
        def __init__(self, a, b):
            self.frame = ttk.Frame(sec_frame)
            self.num = ttk.Label(self.frame, width=3, anchor="e")
            self.slider = RangeSlider(self.frame, self._slid, request_preview)
            self.sv, self.ev = tk.StringVar(), tk.StringVar()
            self.se = ttk.Entry(self.frame, textvariable=self.sv, width=8, justify="center")
            self.ee = ttk.Entry(self.frame, textvariable=self.ev, width=8, justify="center")
            for ent in (self.se, self.ee):
                ent.bind("<Return>", self._typed)
                ent.bind("<FocusOut>", self._typed)
            self.btns = [ttk.Button(self.frame, text=txt, width=3, command=cmd) for txt, cmd in
                         (("▲", lambda: move(self, -1)), ("▼", lambda: move(self, 1)),
                          ("+", lambda: add_section(after=self)), ("✕", lambda: remove(self)))]
            self.num.pack(side="left")
            self.slider.pack(side="left", fill="x", expand=True, padx=(4, 6))
            self.se.pack(side="left")
            ttk.Label(self.frame, text="→").pack(side="left", padx=2)
            self.ee.pack(side="left", padx=(0, 6))
            for btn in self.btns:
                btn.pack(side="left", padx=1)
            self.set(a, b)

        def set(self, a, b):
            dur = state["info"]["duration"] if state["info"] else max(b, 1)
            self.slider.set(a, b, dur)
            self._slid(a, b)

        def _slid(self, a, b):
            self.sv.set(fmt(a))
            self.ev.set(fmt(b))
            update_summary()

        def _typed(self, _e=None):
            try:
                a, b = parse_time(self.sv.get()), parse_time(self.ev.get())
                dur = self.slider.dur
                a, b = min(max(a, 0), dur), min(max(b, 0), dur)
                if b - a < 0.1:
                    raise ValueError
                self.set(a, b)
                request_preview(a)
            except ValueError:
                self._slid(self.slider.a, self.slider.b)  # revert

        def range(self):
            return self.slider.a, self.slider.b

    def relayout():
        for s in sections:
            s.frame.pack_forget()
        for i, s in enumerate(sections):
            s.num.config(text=f"{i + 1}.")
            s.frame.pack(fill="x", pady=3)
            s.btns[3].config(state="normal" if len(sections) > 1 else "disabled")
        update_summary()

    def add_section(after=None):
        if not state["info"]:
            messagebox.showinfo("ClipShrink", "Load a video first.")
            return
        dur = state["info"]["duration"]
        ref = after or (sections[-1] if sections else None)
        a = ref.range()[1] if ref else 0.0
        if a >= dur - 0.5:
            a = 0.0
        s = Section(a, min(dur, a + max(5.0, (dur - a) / 2)) if ref else dur)
        sections.insert(sections.index(after) + 1 if after else len(sections), s)
        relayout()

    def remove(s):
        if len(sections) > 1:
            sections.remove(s)
            s.frame.destroy()
            relayout()

    def move(s, d):
        i = sections.index(s)
        j = i + d
        if 0 <= j < len(sections):
            sections[i], sections[j] = sections[j], sections[i]
            relayout()

    # ------------------------------------------------------------ layout
    outer = ttk.Frame(root, padding=12)
    outer.pack(fill="both", expand=True)
    top = ttk.Frame(outer)
    top.pack(fill="x")

    # drop zone
    drop = tk.Canvas(top, width=360, height=150, bg=BG, highlightthickness=0, cursor="hand2")
    drop.pack(side="left", anchor="n")

    def draw_drop(hover=False):
        drop.delete("all")
        drop.create_rectangle(4, 4, 356, 146, dash=(6, 4), outline=ACCENT if hover else "#9aa0a6", width=2)
        name = os.path.basename(state["src"]) if state["src"] else None
        drop.create_text(180, 58, text="Drop a video here" if dnd else "Click to choose a video",
                         font=("Segoe UI", 12, "bold"), fill="#3c4043")
        drop.create_text(180, 84, text=name or ("or click to browse" if dnd else ""),
                         font=("Segoe UI", 9), fill="#5f6368", width=330)

    drop.bind("<Button-1>", lambda e: browse())
    drop.bind("<Enter>", lambda e: draw_drop(True))
    drop.bind("<Leave>", lambda e: draw_drop(False))
    ttk.Button(top, text="Browse…", command=lambda: browse()).pack(side="left", anchor="n", padx=10)

    # info panel
    info_box = ttk.Frame(top)
    info_box.pack(side="left", fill="both", expand=True, padx=(10, 0))
    info_var = tk.StringVar(value="No video loaded.")
    ttk.Label(info_box, textvariable=info_var, font=("Segoe UI", 10), justify="left").pack(anchor="w")
    preview_lbl = ttk.Label(info_box)
    preview_lbl.pack(anchor="w", pady=(6, 0))

    ttk.Separator(outer).pack(fill="x", pady=10)
    hdr = ttk.Frame(outer)
    hdr.pack(fill="x")
    ttk.Label(hdr, text="Sections (joined top to bottom)", font=("Segoe UI", 10, "bold")).pack(side="left")
    ttk.Button(hdr, text="+ Add section", command=lambda: add_section()).pack(side="right")
    sec_frame = ttk.Frame(outer)
    sec_frame.pack(fill="x", pady=(6, 0))

    ttk.Separator(outer).pack(fill="x", pady=10)
    opts = ttk.Frame(outer)
    opts.pack(fill="x")
    size_var, res_var, out_var = tk.StringVar(value="10"), tk.StringVar(value="Auto"), tk.StringVar()
    ttk.Label(opts, text="Max size (MB)").pack(side="left")
    ttk.Combobox(opts, textvariable=size_var, values=["10", "50", "500"], width=6).pack(side="left", padx=(4, 14))
    ttk.Label(opts, text="Resolution").pack(side="left")
    ttk.Combobox(opts, textvariable=res_var, values=["Auto", "Original", "1080p", "720p", "480p"],
                 state="readonly", width=9).pack(side="left", padx=(4, 14))
    ttk.Label(opts, text="Save as").pack(side="left")
    ttk.Entry(opts, textvariable=out_var).pack(side="left", fill="x", expand=True, padx=4)
    ttk.Button(opts, text="…", width=3, command=lambda: choose_out()).pack(side="left")

    bottom = ttk.Frame(outer)
    bottom.pack(fill="x", pady=(10, 0))
    bar = ttk.Progressbar(bottom, maximum=1000)
    bar.pack(fill="x")
    row = ttk.Frame(bottom)
    row.pack(fill="x", pady=(6, 0))
    status_var = tk.StringVar(value="")
    ttk.Label(row, textvariable=status_var).pack(side="left")
    open_btn = ttk.Button(row, text="Show file", state="disabled", command=lambda: show_file())
    open_btn.pack(side="right")
    go_btn = ttk.Button(row, text="Shrink it", command=lambda: go())
    go_btn.pack(side="right", padx=6)

    # ------------------------------------------------------------ behaviour
    def ui(fn, *a):
        root.after(0, fn, *a)

    def update_summary():
        if not state["info"]:
            return
        i = state["info"]
        kept = sum(b - a for a, b in (s.range() for s in sections))
        fps = " · %.0f fps" % i["fps"] if i["fps"] else ""
        info_var.set(
            f"{os.path.basename(state['src'])}\n"
            f"{i['size'] / 1_000_000:.1f} MB · {fmt(i['duration'])} · {i['width']}×{i['height']}{fps}\n"
            f"Keeping {fmt(kept)} across {len(sections)} section(s)"
        )

    def request_preview(t):
        if not state["src"]:
            return
        state["preview_job"] += 1
        job, src = state["preview_job"], state["src"]

        def work():
            try:
                grab_frame(src, min(t, state["info"]["duration"] - 0.05), preview_png)
            except Exception:
                return
            if job == state["preview_job"]:
                ui(show_preview)

        threading.Thread(target=work, daemon=True).start()

    def show_preview():
        try:
            img = tk.PhotoImage(file=preview_png)
            preview_lbl.config(image=img)
            preview_lbl.image = img
        except tk.TclError:
            pass

    def load(path):
        path = path.strip().strip("{}").strip('"')
        if not os.path.isfile(path):
            return
        if not shutil.which("ffprobe"):
            messagebox.showerror("ClipShrink", "ffmpeg not found on PATH.\nInstall with: winget install Gyan.FFmpeg")
            return
        try:
            info = probe(path)
        except Exception as e:
            messagebox.showerror("ClipShrink", f"Couldn't read that video:\n{e}")
            return
        state.update(src=path, info=info)
        out_var.set(default_output(path))
        for s in sections:
            s.frame.destroy()
        sections.clear()
        sections.append(Section(0.0, info["duration"]))
        relayout()
        draw_drop()
        status_var.set("")
        bar["value"] = 0
        request_preview(0)

    def browse():
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.mov *.avi *.webm *.flv"), ("All", "*.*")])
        if p:
            load(p)

    def choose_out():
        p = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")],
                                         initialfile=os.path.basename(out_var.get() or "clip_discord.mp4"))
        if p:
            out_var.set(p)

    def show_file():
        if state["out"] and os.name == "nt":
            subprocess.run(["explorer", "/select,", os.path.normpath(state["out"])])

    def go():
        if not state["src"]:
            messagebox.showerror("ClipShrink", "Load a video first.")
            return
        try:
            mb = float(size_var.get())
        except ValueError:
            messagebox.showerror("ClipShrink", "Max size must be a number.")
            return
        dst = out_var.get().strip() or default_output(state["src"])
        if os.path.abspath(dst) == os.path.abspath(state["src"]):
            messagebox.showerror("ClipShrink", "Output can't overwrite the source video.")
            return
        segs = [s.range() for s in sections]
        go_btn.config(state="disabled")
        open_btn.config(state="disabled")
        bar["value"] = 0

        def work():
            try:
                size, total, h = shrink(state["src"], dst, segs, mb, res_var.get(),
                                        on_progress=lambda f: ui(bar.config, {"value": f * 1000}),
                                        on_status=lambda s: ui(status_var.set, s))
                state["out"] = dst
                ui(status_var.set, f"Done: {os.path.basename(dst)} — {size:.2f} MB, {fmt(total)}, {h}p")
                ui(open_btn.config, {"state": "normal"})
            except Exception as e:
                ui(status_var.set, "Failed.")
                ui(messagebox.showerror, "ClipShrink", str(e)[-800:])
            finally:
                ui(go_btn.config, {"state": "normal"})

        threading.Thread(target=work, daemon=True).start()

    if dnd:
        drop.drop_target_register(DND_FILES)
        drop.dnd_bind("<<DropEnter>>", lambda e: (draw_drop(True), e.action)[1])
        drop.dnd_bind("<<DropLeave>>", lambda e: (draw_drop(False), e.action)[1])
        drop.dnd_bind("<<Drop>>", lambda e: (load(root.tk.splitlist(e.data)[0]), e.action)[1])

    draw_drop()
    if len(sys.argv) > 1:
        root.after(100, load, sys.argv[1])
    root.mainloop()


if __name__ == "__main__":
    main()
