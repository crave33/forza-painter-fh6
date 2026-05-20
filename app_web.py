from __future__ import annotations

import base64
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import webview

from app import (
    MEMORY_SNAPSHOT_LIMIT_MB,
    ROOT,
    SESSION_PATH,
    game_processes,
    load_session_location,
    parse_hex_or_empty,
    render_geometry_json,
    render_source_image,
    session_pid_is_live,
)
from generator_backend import (
    GENERATOR_EXE,
    best_geometry_jsons,
    build_generator_command,
    generated_jsons,
    generated_preview_files,
    generator_preview_path,
    load_settings,
    write_custom_settings,
)


WEB_DIR = ROOT / "webapp"
HOST = "127.0.0.1"


def pick_free_port(host=HOST):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def data_url(png_bytes):
    if not png_bytes:
        return ""
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return "data:image/png;base64," + encoded


class StaticHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, _format, *args):
        return


class ForzaPainterApi:
    def __init__(self):
        self.window = None
        self.settings = load_settings()
        self.images = []
        self.json_files = []
        self.outputs = []
        self.events = queue.Queue()
        self.shutdown_event = threading.Event()
        self.process_lock = threading.Lock()
        self.active_processes = set()
        self.generator_process = None
        self.generator_layer_samples = []

    def attach_window(self, window):
        self.window = window

    def initial_state(self):
        return {
            "settings": [
                {
                    "label": item["label"],
                    "description": item.get("description", ""),
                    "values": item.get("values", {}),
                }
                for item in self.settings
            ],
            "selectedProfile": self.settings[min(2, len(self.settings) - 1)]["label"] if self.settings else "",
            "images": [str(path) for path in self.images],
            "jsonFiles": [str(path) for path in self.json_files],
            "processes": self.refresh_processes(),
            "status": "Ready",
            "progress": "",
        }

    def poll_events(self):
        items = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        return items

    def _emit(self, kind, payload):
        self.events.put({"kind": kind, "payload": payload})

    def _log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._emit("log", f"[{timestamp}] {message}")

    def _selected_setting(self, label):
        for item in self.settings:
            if item["label"] == label:
                return item
        return self.settings[0] if self.settings else None

    def _effective_setting(self, payload):
        setting = self._selected_setting(payload.get("selectedProfile", ""))
        if not setting or not payload.get("useCustom"):
            return setting
        custom = payload.get("custom", {}) or {}
        values = {
            "stopAt": custom.get("stopAt", ""),
            "maxResolution": custom.get("maxResolution", ""),
            "randomSamples": custom.get("randomSamples", ""),
            "mutatedSamples": custom.get("mutatedSamples", ""),
            "saveAt": custom.get("saveAt", ""),
        }
        if not values["saveAt"] and values["stopAt"]:
            values["saveAt"] = values["stopAt"]
        return write_custom_settings(setting, values)

    def choose_images(self):
        if not self.window:
            return self.add_image_paths([])
        paths = self.window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("Images (*.png;*.jpg;*.jpeg;*.bmp)", "All files (*.*)"),
        )
        return self.add_image_paths(paths or [])

    def add_image_paths(self, paths):
        for item in paths or []:
            path = Path(item)
            if path.exists() and path not in self.images:
                self.images.append(path)
        preview = data_url(render_source_image(self.images[-1])) if self.images else ""
        return {"images": [str(path) for path in self.images], "preview": preview}

    def remove_images(self):
        self.images.clear()
        self._log("Cleared input images.")
        return {"images": []}

    def preview_image(self, path):
        return data_url(render_source_image(Path(path)))

    def choose_json(self):
        if not self.window:
            return self.add_json_paths([])
        paths = self.window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("Geometry JSON (*.json)", "All files (*.*)"),
        )
        return self.add_json_paths(paths or [])

    def add_json_paths(self, paths):
        for item in paths or []:
            path = Path(item)
            if path.exists() and path not in self.json_files:
                self.json_files.append(path)
        preview = data_url(render_geometry_json(self.json_files[-1])) if self.json_files else ""
        return {"jsonFiles": [str(path) for path in self.json_files], "preview": preview}

    def preview_json(self, path):
        return data_url(render_geometry_json(Path(path)))

    def use_generated_outputs(self):
        for path in self.outputs:
            if path not in self.json_files:
                self.json_files.append(path)
        return {"jsonFiles": [str(path) for path in self.json_files]}

    def refresh_processes(self):
        return game_processes()

    def _register_process(self, proc):
        with self.process_lock:
            self.active_processes.add(proc)

    def _unregister_process(self, proc):
        with self.process_lock:
            self.active_processes.discard(proc)

    def _terminate_process(self, proc):
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=5,
                )
            else:
                proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def start_generate(self, payload):
        if not self.images:
            self._log("No images selected.")
            return {"ok": False}
        if self.generator_process and self.generator_process.poll() is None:
            self._log("Generator is already running.")
            return {"ok": False}
        setting = self._effective_setting(payload or {})
        if not setting:
            self._log("No quality profile selected.")
            return {"ok": False}
        if not GENERATOR_EXE.exists():
            self._log(f"Missing generator: {GENERATOR_EXE}")
            return {"ok": False}
        self.shutdown_event.clear()
        self._emit("progress", "")
        self._emit("status", "Running")
        threading.Thread(target=self._generate_worker, args=(setting,), daemon=True).start()
        return {"ok": True}

    def stop_generate(self):
        self.shutdown_event.set()
        proc = self.generator_process
        if proc and proc.poll() is None:
            self._log("Stopping generator...")
            self._terminate_process(proc)
            self._emit("progress", "Stopped")
            self._emit("status", "Failed")
            return {"ok": True}
        self._log("No generator is running.")
        return {"ok": False}

    def _reset_generator_eta(self):
        self.generator_layer_samples = []

    def _format_duration(self, seconds):
        seconds = max(0, int(round(seconds)))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes:02d}m"
        if minutes:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"

    def _format_layer_progress(self, current, total):
        now = time.time()
        if self.generator_layer_samples and current <= self.generator_layer_samples[-1][0]:
            self._reset_generator_eta()
        self.generator_layer_samples.append((current, now))
        if len(self.generator_layer_samples) > 200:
            self.generator_layer_samples = self.generator_layer_samples[-200:]
        eta = ""
        if total > current and len(self.generator_layer_samples) >= 2:
            first_layer, first_time = self.generator_layer_samples[0]
            last_layer, last_time = self.generator_layer_samples[-1]
            layers_done = last_layer - first_layer
            elapsed = last_time - first_time
            if layers_done > 0 and elapsed > 0:
                remaining = (elapsed / layers_done) * (total - current)
                finish_at = datetime.fromtimestamp(now + remaining).strftime("%H:%M:%S")
                eta = f" | ETA {finish_at} ({self._format_duration(remaining)} left)"
        return f"Generated layer {current}/{total}{eta}"

    def _friendly_generator_line(self, line):
        text = (line or "").strip()
        if not text:
            return None
        progress = re.match(r"\[(\d+)/(\d+)\]\s+(.*)", text)
        if progress:
            current, total, detail = progress.groups()
            if "Added rotated ellipse" in detail:
                return self._format_layer_progress(int(current), int(total))
            if "Saved geometry checkpoint" in detail:
                return f"Saved JSON checkpoint {current}/{total}"
            if "Saved preview snapshot" in detail:
                return f"Updated preview {current}/{total}"
            return None
        if text.startswith("Loaded image:") or text.startswith("Settings:") or text == "FINISHED":
            return text
        if "error" in text.lower() or "failed" in text.lower() or "panic" in text.lower():
            return text
        return None

    def _queue_generator_message(self, friendly, last_message):
        if not friendly or friendly == last_message:
            return last_message
        if friendly.startswith("Generated layer "):
            self._emit("progress", friendly)
            self._log(friendly)
            return friendly
        if friendly == "FINISHED":
            self._emit("progress", friendly)
        self._log(friendly)
        return friendly

    def _generate_worker(self, setting):
        try:
            self._log(f"Selected profile: {setting['path'].name}")
            for image_path in list(self.images):
                if self.shutdown_event.is_set():
                    self._emit("status", "Failed")
                    return
                before = {path.resolve() for path in generated_jsons(image_path)}
                preview_path = generator_preview_path(image_path)
                if preview_path.exists():
                    try:
                        preview_path.unlink()
                    except OSError:
                        pass
                self._log(f"Generating: {image_path}")
                self._emit("preview", data_url(render_source_image(image_path)))
                self._reset_generator_eta()
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                proc = subprocess.Popen(
                    build_generator_command(image_path, setting),
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
                self._register_process(proc)
                self.generator_process = proc
                output_queue = queue.Queue()

                def read_output():
                    try:
                        for raw_line in proc.stdout:
                            output_queue.put(raw_line)
                    finally:
                        output_queue.put(None)

                threading.Thread(target=read_output, daemon=True).start()
                last_preview_mtime = None
                last_message = None

                def drain_output():
                    nonlocal last_message
                    while True:
                        try:
                            raw_line = output_queue.get_nowait()
                        except queue.Empty:
                            break
                        if raw_line is not None:
                            last_message = self._queue_generator_message(
                                self._friendly_generator_line(raw_line),
                                last_message,
                            )

                try:
                    while proc.poll() is None:
                        if self.shutdown_event.is_set():
                            self._terminate_process(proc)
                            self._emit("status", "Failed")
                            return
                        drain_output()
                        previews = generated_preview_files(image_path)
                        if previews:
                            newest = previews[0]
                            mtime = newest.stat().st_mtime
                            if mtime != last_preview_mtime:
                                last_preview_mtime = mtime
                                self._emit("preview", data_url(newest.read_bytes()))
                        time.sleep(0.1)
                    drain_output()
                finally:
                    self._unregister_process(proc)
                    if self.generator_process is proc:
                        self.generator_process = None
                if proc.returncode != 0:
                    self._log(f"Generator exited with code {proc.returncode}.")
                    self._emit("status", "Failed")
                    return
                after = generated_jsons(image_path)
                new_outputs = best_geometry_jsons([path for path in after if path.resolve() not in before])
                if not new_outputs and after:
                    new_outputs = best_geometry_jsons(after[:1])
                if not new_outputs:
                    self._log("Generator finished but no JSON output was found.")
                    self._emit("status", "Failed")
                    return
                for output in new_outputs:
                    if output not in self.outputs:
                        self.outputs.append(output)
                    if output not in self.json_files:
                        self.json_files.append(output)
                    self._log(f"Generated: {output}")
                    self._emit("jsonFiles", [str(path) for path in self.json_files])
                    preview_files = generated_preview_files(image_path)
                    if preview_files:
                        self._emit("preview", data_url(preview_files[0].read_bytes()))
                    else:
                        self._emit("preview", data_url(render_geometry_json(output)))
            self._emit("status", "Done")
        except Exception as exc:
            self._log(f"Generator failed: {exc}")
            self._emit("status", "Failed")
        finally:
            self.generator_process = None

    def open_output_folder(self):
        folder = None
        if self.outputs:
            folder = self.outputs[-1].parent
        elif self.images:
            folder = self.images[-1].parent
        if folder and folder.exists():
            os.startfile(folder)
            return {"ok": True}
        self._log("No output folder is available yet.")
        return {"ok": False}

    def _friendly_subprocess_line(self, line):
        if not line:
            return None
        raw = line.strip()
        lower = raw.lower()
        noisy_parts = (
            "base:",
            "candidate score=",
            "layout candidate",
            "table[",
            "ptr=0x",
            "count=0x",
            "tablefield=",
            "wrote fh6 session location",
            "fh6 layout-count scan checked",
            "process: forzahorizon",
            "current values:",
            "loaded ",
            "descriptor @",
            "info found:",
            "vtp found:",
        )
        if any(part in lower for part in noisy_parts):
            return None
        if "fast fh6 layer group candidates:" in lower or "cliverylayer table found" in lower:
            return "FH6 template located and verified."
        if "no safe fh6 layer group" in lower:
            return "Stopped before writing because no safe FH6 template was found."
        if "auto-locating fh6 layer count/table" in lower:
            return "Finding current FH6 template..."
        if raw.startswith("Writing layer") or raw == "DONE!" or raw.startswith("The ideal background color"):
            return raw
        if "openprocess" in lower or "error" in lower or "failed" in lower or "traceback" in lower:
            return raw
        if raw.startswith("<class 'SystemExit'>") or raw.startswith("SystemExit: 0"):
            return None
        return raw

    def run_subprocess(self, cmd, timeout=None):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = os.environ.copy()
        env.update({"FORZA_PAINTER_NO_ELEVATE": "1", "FORZA_PAINTER_NO_PAUSE": "1"})
        proc = subprocess.Popen(
            [str(x) for x in cmd],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
            env=env,
        )
        self._register_process(proc)
        started = time.time()
        try:
            while True:
                line = proc.stdout.readline()
                if line:
                    friendly = self._friendly_subprocess_line(line.rstrip())
                    if friendly:
                        self._log(friendly)
                if proc.poll() is not None:
                    break
                if timeout and time.time() - started > timeout:
                    self._terminate_process(proc)
                    self._log(f"Timed out after {timeout} seconds.")
                    return 124
                time.sleep(0.05)
            for line in proc.stdout.read().splitlines():
                friendly = self._friendly_subprocess_line(line.rstrip())
                if friendly:
                    self._log(friendly)
            return proc.returncode
        finally:
            self._unregister_process(proc)

    def start_import(self, payload):
        if not self.json_files:
            self._log("No JSON files selected.")
            return {"ok": False}
        self._emit("status", "Running")
        threading.Thread(target=self._import_worker, args=(payload or {},), daemon=True).start()
        return {"ok": True}

    def _import_worker(self, payload):
        game = payload.get("game") or "fh6"
        pid = payload.get("pid") or None
        layer_count = str(payload.get("layerCount") or "").strip()
        count_address = parse_hex_or_empty(payload.get("countAddress"))
        table_address = parse_hex_or_empty(payload.get("tableAddress"))
        try:
            pid = int(pid) if pid else None
        except ValueError:
            pid = None
        if not count_address and not table_address and game == "fh6":
            session = load_session_location()
            matches = session and str(session.get("layer_count", "")) == str(layer_count)
            if session and matches and session_pid_is_live(session, game) and (not pid or int(pid) == int(session.get("pid", -1))):
                pid = int(session["pid"])
                count_address = "0x{:x}".format(int(session["count_address"]))
                table_address = "0x{:x}".format(int(session["table_address"]))
                self._log("FH6 template located and verified.")
            elif pid and layer_count:
                self._log("Finding current FH6 template...")
                cmd = [
                    sys.executable,
                    ROOT / "fh6_probe.py",
                    "--game",
                    game,
                    "--pid",
                    str(pid),
                    "--layer-count",
                    str(layer_count),
                    "--auto-locate",
                    "--write-session",
                    SESSION_PATH,
                    "--limit-mb",
                    str(MEMORY_SNAPSHOT_LIMIT_MB),
                    "--max-matches",
                    "500000",
                    "--inspect-radius",
                    "0x800",
                    "--max-seconds",
                    "45",
                ]
                self.run_subprocess(cmd, timeout=70)
                session = load_session_location()
                if session and str(session.get("layer_count", "")) == str(layer_count) and session_pid_is_live(session, game):
                    count_address = "0x{:x}".format(int(session["count_address"]))
                    table_address = "0x{:x}".format(int(session["table_address"]))
                    self._log("FH6 template located and verified.")
                else:
                    self._emit("status", "Failed")
                    return
        for path in list(self.json_files):
            cmd = [sys.executable, ROOT / "main.py", "--game", game, "--no-preview"]
            if pid:
                cmd.extend(["--pid", str(pid)])
            if count_address:
                cmd.extend(["--layer-count-address", count_address])
            if table_address:
                cmd.extend(["--layer-table-address", table_address])
            if game == "fh6" and layer_count:
                cmd.extend(["--layer-count-value", str(layer_count)])
            cmd.append(path)
            code = self.run_subprocess(cmd)
            if code != 0:
                self._emit("status", "Failed")
                return
        self._emit("status", "Done")


def main():
    if not WEB_DIR.exists():
        raise SystemExit(f"Missing web UI folder: {WEB_DIR}")
    api = ForzaPainterApi()
    port = pick_free_port()
    server = ThreadingHTTPServer((HOST, port), StaticHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="forza-painter-web", daemon=True).start()

    window = webview.create_window(
        "forza-painter FH6",
        url=f"http://{HOST}:{port}/",
        width=1280,
        height=860,
        min_size=(1100, 720),
        background_color="#0A0D10",
        js_api=api,
        text_select=True,
    )
    api.attach_window(window)

    def closed():
        api.shutdown_event.set()
        if api.generator_process:
            api._terminate_process(api.generator_process)
        server.shutdown()
        server.server_close()

    window.events.closed += closed
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
    webview.start(gui="edgechromium", debug=True, private_mode=False, storage_path=str(ROOT / ".webview_forza"))


if __name__ == "__main__":
    main()
