from __future__ import annotations

import ctypes
import csv
import os
import psutil
import queue
import statistics
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from collections import defaultdict, deque
from ctypes import wintypes
from pathlib import Path

# Use physical screen coordinates so Windows display scaling cannot offset the widget.
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

APP_NAME = "MonitorWidget"
MUTEX_NAME = "Local\\MinimalSystemMonitor.Singleton"
STOP_EVENT_NAME = "Local\\MinimalSystemMonitor.Stop"
BG, PANEL, TEXT, MUTED, TRACK = "#111318", "#181B22", "#F4F6FA", "#7F8798", "#272B34"
COLORS = ("#71A7FF", "#B38BFF", "#54D6A1", "#FFB45E")


class FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    @property
    def value(self):
        return (self.high << 32) | self.low


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
        (name, ctypes.c_ulonglong) for name in (
            "total_phys", "avail_phys", "total_page", "avail_page",
            "total_virtual", "avail_virtual", "avail_extended"
        )
    ]


class RATIO(ctypes.Structure):
    _fields_ = [("numerator", wintypes.UINT), ("denominator", wintypes.UINT)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


U64, I64, U32 = ctypes.c_ulonglong, ctypes.c_longlong, wintypes.UINT


class DWM_TIMING_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", U32), ("rateRefresh", RATIO), ("qpcRefreshPeriod", I64),
        ("rateCompose", RATIO), ("qpcVBlank", I64), ("cRefresh", U64),
        ("cDXRefresh", U32), ("qpcCompose", I64), ("cFrame", U64),
        ("cDXPresent", U32), ("cRefreshFrame", U64), ("cFrameSubmitted", U64),
        ("cDXPresentSubmitted", U32), ("cFrameConfirmed", U64),
        ("cDXPresentConfirmed", U32), ("cRefreshConfirmed", U64),
        ("cDXRefreshConfirmed", U32), ("cFramesLate", U64),
        ("cFramesOutstanding", U32), ("cFrameDisplayed", U64),
        ("qpcFrameDisplayed", I64), ("cRefreshFrameDisplayed", U64),
        ("cFrameComplete", U64), ("qpcFrameComplete", I64),
        ("cFramePending", U64), ("qpcFramePending", I64),
        ("cFramesDisplayed", U64), ("cFramesComplete", U64),
        ("cFramesPending", U64), ("cFramesAvailable", U64),
        ("cFramesDropped", U64), ("cFramesMissed", U64),
        ("cRefreshNextDisplayed", U64), ("cRefreshNextPresented", U64),
        ("cRefreshesDisplayed", U64), ("cRefreshesPresented", U64),
        ("cRefreshStarted", U64), ("cPixelsReceived", U64),
        ("cPixelsDrawn", U64), ("cBuffersEmpty", U64),
    ]


def gb(value):
    return f"{value / 1024**3:.1f} GB"


def network_speed_text(bytes_per_second):
    if bytes_per_second >= 1024 * 1024:
        return f"{bytes_per_second / (1024 * 1024):.1f} MB/s"
    return f"{bytes_per_second / 1024:.1f} KB/s"


class NetworkSampler:
    """Measure per-second traffic on active, non-loopback network adapters."""

    def __init__(self):
        self.previous = None

    def sample(self):
        counters = psutil.net_io_counters(pernic=True)
        stats = psutil.net_if_stats()
        active = [name for name in counters if name in stats and stats[name].isup]
        physical = [name for name in active if not any(marker in name.lower() for marker in (
            "loopback", "vmware", "virtualbox", "vEthernet".lower(), "hyper-v", "bluetooth",
        ))]
        selected = physical or [name for name in active if "loopback" not in name.lower()]
        if not selected:
            self.previous = None
            return "—", "—"
        received = sum(counters[name].bytes_recv for name in selected)
        sent = sum(counters[name].bytes_sent for name in selected)
        current = (time.monotonic(), received, sent)
        if self.previous is None:
            self.previous = current
            return "0.0 KB/s", "0.0 KB/s"
        old_time, old_received, old_sent = self.previous
        self.previous = current
        elapsed = max(current[0] - old_time, 0.001)
        download = max(0, received - old_received) / elapsed
        upload = max(0, sent - old_sent) / elapsed
        return network_speed_text(download), network_speed_text(upload)


class Sampler:
    def __init__(self):
        self.kernel = ctypes.windll.kernel32
        self.previous_cpu = None

    def cpu(self):
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not self.kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return 0.0
        current = (kernel.value + user.value - idle.value, kernel.value + user.value)
        if self.previous_cpu is None:
            self.previous_cpu = current
            return 0.0
        old_busy, old_total = self.previous_cpu
        self.previous_cpu = current
        return max(0.0, min(100.0, (current[0] - old_busy) * 100 / max(1, current[1] - old_total)))

    def memory(self):
        info = MEMORYSTATUSEX()
        info.length = ctypes.sizeof(info)
        self.kernel.GlobalMemoryStatusEx(ctypes.byref(info))
        return float(info.load), f"{gb(info.total_phys - info.avail_phys)} / {gb(info.total_phys)}"



def bundled_path(relative):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


class PresentMonSampler:
    """Read real application presents from Intel PresentMon's ETW stream."""

    GAME_NAMES = {"league of legends.exe", "leagueclient.exe"}

    def __init__(self):
        self.samples = defaultdict(lambda: deque(maxlen=240))
        self.hardware_apps = set()
        self.process = None
        self.running = True
        threading.Thread(target=self._capture, daemon=True).start()

    def _capture(self):
        executable = bundled_path("tools/PresentMon.exe")
        if not executable.exists():
            return
        command = [
            str(executable), "--output_stdout", "--no_console_stats",
            "--no_track_gpu", "--no_track_input", "--v1_metrics",
            "--session_name", "MinimalSystemMonitor", "--stop_existing_session",
        ]
        try:
            self.process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            header = None
            for raw_line in self.process.stdout:
                if not self.running:
                    break
                line = raw_line.strip()
                if line.startswith("Application,ProcessID,"):
                    header = next(csv.reader([line]))
                    continue
                if not header or not line or line.startswith("warning:"):
                    continue
                try:
                    values = next(csv.reader([line]))
                    row = dict(zip(header, values))
                    app = row["Application"].strip()
                    interval = float(row["msBetweenPresents"])
                    if app == "<unknown>" or not 1.0 <= interval <= 1000.0:
                        continue
                    mode = row.get("PresentMode", "")
                    if mode.startswith("Hardware:"):
                        self.hardware_apps.add(app.lower())
                    self.samples[app.lower()].append((time.monotonic(), interval))
                except (KeyError, ValueError, csv.Error):
                    continue
        except (OSError, subprocess.SubprocessError):
            self.process = None

    def fps(self):
        now = time.monotonic()
        candidates = []
        for app, values in list(self.samples.items()):
            recent = [interval for stamp, interval in values if now - stamp <= 1.5]
            if len(recent) < 3:
                continue
            priority = 2 if app in self.GAME_NAMES else 1 if app in self.hardware_apps else 0
            if priority:
                fps = 1000.0 / statistics.median(recent[-120:])
                candidates.append((priority, len(recent), fps))
        if not candidates:
            return "—"
        _priority, _count, fps = max(candidates, key=lambda item: (item[0], item[1]))
        return str(round(max(1.0, min(999.0, fps))))

    def stop(self):
        self.running = False
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass


class TrayIcon:
    WM_TRAY = 0x8001
    CMD_TOGGLE = 1001
    CMD_EXIT = 1002
    CMD_TOP_LEFT = 1003
    CMD_TOP_RIGHT = 1004

    def __init__(self, actions):
        self.actions = actions
        self.hwnd = None
        self.nid = None
        self.icon = None
        self.wndproc = WNDPROC(self._window_proc)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        user32, shell32, kernel32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.LoadIconW.restype = wintypes.HICON
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                      wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                       ctypes.c_size_t, wintypes.LPCWSTR]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, ctypes.c_void_p]
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                              ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        instance = kernel32.GetModuleHandleW(None)
        class_name = f"MinimalSystemMonitorTray_{os.getpid()}"
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self.wndproc
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(window_class))
        self.hwnd = user32.CreateWindowExW(0, class_name, "System Monitor Tray", 0,
                                            0, 0, 0, 0, None, None, instance, None)
        icon_path = bundled_path("assets/monitor_icon.ico")
        self.icon = user32.LoadImageW(None, str(icon_path), 1, 0, 0, 0x10 | 0x40)
        if not self.icon:
            self.icon = user32.LoadIconW(None, ctypes.c_void_p(32512))
        self.nid = NOTIFYICONDATAW()
        self.nid.cbSize = ctypes.sizeof(self.nid)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uFlags = 0x1 | 0x2 | 0x4
        self.nid.uCallbackMessage = self.WM_TRAY
        self.nid.hIcon = self.icon
        self.nid.szTip = "系统资源监控"
        shell32.Shell_NotifyIconW(0, ctypes.byref(self.nid))
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _window_proc(self, hwnd, message, wparam, lparam):
        user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
        if getattr(self, "taskbar_created", None) == message and self.nid:
            shell32.Shell_NotifyIconW(0, ctypes.byref(self.nid))
            return 0
        if message == self.WM_TRAY:
            event = int(lparam) & 0xFFFF
            if event == 0x0203:
                self.actions.put("toggle")
            elif event == 0x0205:
                point = POINT()
                user32.GetCursorPos(ctypes.byref(point))
                menu = user32.CreatePopupMenu()
                user32.AppendMenuW(menu, 0, self.CMD_TOGGLE, "显示 / 隐藏组件")
                user32.AppendMenuW(menu, 0, self.CMD_TOP_LEFT, "显示在左上角")
                user32.AppendMenuW(menu, 0, self.CMD_TOP_RIGHT, "显示在右上角")
                user32.AppendMenuW(menu, 0x800, 0, None)
                user32.AppendMenuW(menu, 0, self.CMD_EXIT, "退出")
                user32.SetForegroundWindow(hwnd)
                command = user32.TrackPopupMenu(menu, 0x100 | 0x80, point.x, point.y, 0, hwnd, None)
                user32.DestroyMenu(menu)
                if command == self.CMD_TOGGLE:
                    self.actions.put("toggle")
                elif command == self.CMD_TOP_LEFT:
                    self.actions.put("top_left")
                elif command == self.CMD_TOP_RIGHT:
                    self.actions.put("top_right")
                elif command == self.CMD_EXIT:
                    self.actions.put("exit")
            return 0
        if message == 0x0010:
            if self.nid:
                shell32.Shell_NotifyIconW(2, ctypes.byref(self.nid))
            user32.DestroyWindow(hwnd)
            return 0
        if message == 0x0002:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def stop(self):
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)


def read_gpu():
    command = ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        util, used, total = (float(x.strip()) for x in result.stdout.strip().splitlines()[0].split(","))
        return util, used * 100 / max(total, 1), f"{used / 1024:.1f} / {total / 1024:.1f} GB"
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None, None, "不可用"


class Metric:
    def __init__(self, canvas, y, label, color):
        self.canvas, self.y = canvas, y
        canvas.create_text(18, y, text=label, anchor="w", fill=MUTED, font=("Segoe UI", 9))
        self.text = canvas.create_text(102, y, text="—", anchor="e", fill=color, font=("Segoe UI Semibold", 10))

    def set(self, percent, detail=None):
        if percent is None:
            self.canvas.itemconfigure(self.text, text=detail or "—")
            return
        percent = max(0.0, min(100.0, percent))
        label = f"{percent:.0f}%" + (f"  ·  {detail}" if detail else "")
        self.canvas.itemconfigure(self.text, text=label)


class Widget:
    WIDTH, HEIGHT = 120, 190

    def __init__(self, stop_event=None):
        self.root = tk.Tk()
        self.root.title("系统监控")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.60)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.sampler, self.gpu = Sampler(), read_gpu()
        self.network_sampler = NetworkSampler()
        self.fps_sampler = PresentMonSampler()
        self.stop_event = stop_event
        self.gpu_queue = queue.Queue(maxsize=1)
        self.tray_actions = queue.SimpleQueue()
        self.corner = "left"
        self.drag_origin = None
        self.to_corner()
        self.draw()
        self.root.update_idletasks()
        self.lock_overlay()
        self.tray_icon = TrayIcon(self.tray_actions)
        self.update()

    def to_corner(self, corner=None):
        if corner in ("left", "right"):
            self.corner = corner
        work = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(work), 0):
            x = work.left if self.corner == "left" else work.right - self.WIDTH
            y = work.top
        else:
            screen_width = ctypes.windll.user32.GetSystemMetrics(0)
            x = 0 if self.corner == "left" else screen_width - self.WIDTH
            y = 0
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def to_top_left(self):
        self.to_corner("left")

    def to_top_right(self):
        self.to_corner("right")

    def draw(self):
        c = self.canvas = tk.Canvas(self.root, width=self.WIDTH, height=self.HEIGHT, bg=BG, highlightthickness=0)
        c.pack()
        c.create_rectangle(1, 1, self.WIDTH - 1, self.HEIGHT - 1, fill=BG, outline="#292D37")
        self.metrics = [Metric(c, y, label, color) for y, label, color in zip(
            (17, 42, 67, 92), ("CPU", "内存", "GPU", "显存"), COLORS)]
        c.create_line(18, 108, 102, 108, fill="#252932")
        c.create_text(18, 121, text="FPS", anchor="w", fill=MUTED, font=("Segoe UI", 9))
        self.fps_text = c.create_text(102, 121, text="—", anchor="e", fill=TEXT, font=("Segoe UI Semibold", 13))
        c.create_line(18, 140, 102, 140, fill="#252932")
        c.create_text(18, 156, text="↓", anchor="w", fill=MUTED, font=("Segoe UI", 10))
        c.create_text(18, 178, text="↑", anchor="w", fill=MUTED, font=("Segoe UI", 10))
        self.download_text = c.create_text(102, 156, text="—", anchor="e", fill="#71A7FF", font=("Segoe UI Semibold", 9))
        self.upload_text = c.create_text(102, 178, text="—", anchor="e", fill="#54D6A1", font=("Segoe UI Semibold", 9))

    def lock_overlay(self):
        """Make the widget click-through, non-activating, and permanently topmost."""
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        ex_style = user32.GetWindowLongW(hwnd, -20)
        ex_style |= 0x00000020 | 0x00000080 | 0x00080000 | 0x08000000
        user32.SetWindowLongW(hwnd, -20, ex_style)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
        self.root.attributes("-topmost", True)

    def toggle_visibility(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.to_corner()
            self.lock_overlay()
        else:
            self.root.withdraw()

    def shutdown(self):
        self.fps_sampler.stop()
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.destroy()

    def make_menu(self):
        self.menu = tk.Menu(self.root, tearoff=False, bg=PANEL, fg=TEXT, activebackground="#2D3340", activeforeground=TEXT)
        for label, action in (("回到左上角", self.to_top_left), ("切换置顶", self.toggle_top)):
            self.menu.add_command(label=label, command=action)
        self.menu.add_separator()
        self.menu.add_command(label="开机启动：切换", command=self.toggle_autostart)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.root.destroy)

    def bind(self):
        for item in (self.root, self.canvas):
            item.bind("<Button-3>", lambda event: self.menu.tk_popup(event.x_root, event.y_root))
        self.root.bind("<Escape>", lambda _event: self.root.destroy())

    def start_drag(self, event):
        self.drag_origin = event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y()

    def drag(self, event):
        if self.drag_origin:
            mx, my, wx, wy = self.drag_origin
            self.root.geometry(f"+{wx + event.x_root - mx}+{wy + event.y_root - my}")

    def toggle_top(self):
        self.root.attributes("-topmost", not bool(self.root.attributes("-topmost")))

    def toggle_autostart(self):
        path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        launcher = Path(__file__).with_name("启动监控.vbs").resolve()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            try:
                winreg.QueryValueEx(key, APP_NAME)
                winreg.DeleteValue(key, APP_NAME)
                status = "开机启动已关闭"
            except FileNotFoundError:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'wscript.exe "{launcher}"')
                status = "开机启动已开启"
        self.canvas.itemconfigure(self.tip, text=status)
        self.root.after(2200, lambda: self.canvas.itemconfigure(self.tip, text="右键设置"))

    def request_gpu(self):
        def work():
            try:
                self.gpu_queue.put_nowait(read_gpu())
            except queue.Full:
                pass
        threading.Thread(target=work, daemon=True).start()

    def update(self):
        while not self.tray_actions.empty():
            action = self.tray_actions.get_nowait()
            if action == "exit":
                self.shutdown()
                return
            if action == "toggle":
                self.toggle_visibility()
            elif action == "top_left":
                self.to_top_left()
            elif action == "top_right":
                self.to_top_right()
        if self.stop_event and ctypes.windll.kernel32.WaitForSingleObject(self.stop_event, 0) == 0:
            self.shutdown()
            return
        self.to_corner()
        self.lock_overlay()
        cpu, (memory, _mem_detail) = self.sampler.cpu(), self.sampler.memory()
        try:
            self.gpu = self.gpu_queue.get_nowait()
        except queue.Empty:
            pass
        gpu, vram, _vram_detail = self.gpu
        self.metrics[0].set(cpu)
        self.metrics[1].set(memory)
        self.metrics[2].set(gpu, "不可用" if gpu is None else None)
        self.metrics[3].set(vram, "不可用" if vram is None else None)
        self.canvas.itemconfigure(self.fps_text, text=self.fps_sampler.fps())
        download, upload = self.network_sampler.sample()
        self.canvas.itemconfigure(self.download_text, text=download)
        self.canvas.itemconfigure(self.upload_text, text=upload)
        self.request_gpu()
        self.root.after(1000, self.update)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.fps_sampler.stop()
            if self.tray_icon:
                self.tray_icon.stop()


def create_single_instance():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = ctypes.c_void_p
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not mutex or kernel32.GetLastError() == 183:
        return None
    stop_event = kernel32.CreateEventW(None, True, False, STOP_EVENT_NAME)
    return mutex, stop_event


def ensure_autostart():
    """Register the frozen EXE (or source launcher) for the current Windows user."""
    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    if getattr(sys, "frozen", False):
        command = f'"{Path(sys.executable).resolve()}"'
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        command = f'"{pythonw}" "{Path(__file__).resolve()}"'
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("这个小组件当前仅支持 Windows。")
    instance_handles = create_single_instance()
    if instance_handles:
        _mutex, event_handle = instance_handles
        ensure_autostart()
        Widget(event_handle).run()
