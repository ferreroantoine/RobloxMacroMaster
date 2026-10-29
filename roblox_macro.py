# -*- coding: utf-8 -*-
"""
roblox_macro.py - enregistreur / lecteur de macros pour Roblox, compatible Shift Lock.

En Shift Lock, Roblox recentre la souris et ne lit que les deplacements relatifs.
Ce script enregistre donc les deltas BRUTS de la souris (Raw Input, WM_INPUT) et
les rejoue en relatif (SendInput + MOUSEEVENTF_MOVE, sans MOUSEEVENTF_ABSOLUTE).

Dependances : aucune (bibliotheque standard). Python 3.8+ 64 bits, Windows.
Interface graphique (bibliotheque de macros, declencheurs par image) : macro_gui.py.

  F8      demarrer / arreter l'enregistrement (sauvegarde auto dans MACRO_FILE)
  F9      lancer / arreter la lecture (REPEAT fois)
  F10     lancer / arreter la lecture en boucle infinie
  Ctrl+C  quitter (dans cette console)

Les options en ligne de commande sont prioritaires sur les valeurs ci-dessous :
  python roblox_macro.py --debug --speed 1.0 --sens 1.0 --repeat 0 --file macro.json --admin
"""

# ============================ PARAMETRES ============================
MACRO_FILE      = "macro.json"  # relatif au dossier du script
SPEED           = 1.0     # facteur de vitesse : 2.0 = 2x plus vite, 0.5 = 2x plus lent
SENSITIVITY     = 1.0     # multiplicateur des deltas souris a la lecture
REPEAT          = 1       # nombre de lectures ; 0 = boucle infinie
LOOP_DELAY      = 0.0     # pause (secondes) entre deux repetitions
DEBUG           = False   # affiche les deltas captures dans la console
DEBUG_INTERVAL  = 0.10    # regroupement des deltas affiches en debug (secondes)
SWALLOW_HOTKEYS = True    # F8/F9/F10 ne sont pas transmis aux jeux (F9 = console dev Roblox)
RESTORE_START_POS = True  # remet la souris a sa position du debut d'enregistrement avant chaque lecture
HOTKEY_RECORD   = 0x77    # VK_F8
HOTKEY_PLAY     = 0x78    # VK_F9  : lecture (REPEAT fois)
HOTKEY_LOOP     = 0x79    # VK_F10 : lecture en boucle infinie
# ====================================================================

HOTKEYS = (HOTKEY_RECORD, HOTKEY_PLAY, HOTKEY_LOOP)

import argparse
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes as W

# ----------------------------- Win32 --------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
PTR_MASK = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1

WM_INPUT = 0x00FF
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
WH_KEYBOARD_LL = 13

RID_INPUT = 0x10000003
RIM_TYPEMOUSE, RIM_TYPEKEYBOARD = 0, 1
RIDEV_INPUTSINK = 0x00000100
RI_KEY_BREAK, RI_KEY_E0, RI_KEY_E1 = 0x1, 0x2, 0x4
MOUSE_MOVE_ABSOLUTE = 0x01
RI_MOUSE_WHEEL, RI_MOUSE_HWHEEL = 0x0400, 0x0800
MAPVK_VK_TO_VSC = 0

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20

# Marque nos propres SendInput (dwExtraInfo) pour les reconnaitre en Raw Input.
INJECT_TAG = 0x4D414352

ROBLOX_EXES = {"robloxplayerbeta.exe", "robloxstudiobeta.exe", "windows10universal.exe"}

# Raw Input : drapeau -> (nom, appuye)
RAW_BUTTONS = (
    (0x0001, "left", 1), (0x0002, "left", 0),
    (0x0004, "right", 1), (0x0008, "right", 0),
    (0x0010, "middle", 1), (0x0020, "middle", 0),
    (0x0040, "x1", 1), (0x0080, "x1", 0),
    (0x0100, "x2", 1), (0x0200, "x2", 0),
)
# SendInput : (nom, appuye) -> (drapeau, mouseData)
SEND_BUTTONS = {
    ("left", 1): (0x0002, 0), ("left", 0): (0x0004, 0),
    ("right", 1): (0x0008, 0), ("right", 0): (0x0010, 0),
    ("middle", 1): (0x0020, 0), ("middle", 0): (0x0040, 0),
    ("x1", 1): (0x0080, 1), ("x1", 0): (0x0100, 1),
    ("x2", 1): (0x0080, 2), ("x2", 0): (0x0100, 2),
}
BUTTON_FR = {"left": "gauche", "right": "droit", "middle": "molette", "x1": "X1", "x2": "X2"}


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", W.USHORT), ("usUsage", W.USHORT),
                ("dwFlags", W.DWORD), ("hwndTarget", W.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", W.DWORD), ("dwSize", W.DWORD),
                ("hDevice", W.HANDLE), ("wParam", W.WPARAM)]


class _RAWMOUSEBUTTONS(ctypes.Structure):
    _fields_ = [("usButtonFlags", W.USHORT), ("usButtonData", W.USHORT)]


class _RAWMOUSEUNION(ctypes.Union):
    _fields_ = [("ulButtons", W.ULONG), ("b", _RAWMOUSEBUTTONS)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", W.USHORT), ("u", _RAWMOUSEUNION), ("ulRawButtons", W.ULONG),
                ("lLastX", W.LONG), ("lLastY", W.LONG), ("ulExtraInformation", W.ULONG)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", W.USHORT), ("Flags", W.USHORT), ("Reserved", W.USHORT),
                ("VKey", W.USHORT), ("Message", W.UINT), ("ExtraInformation", W.ULONG)]


class _RAWINPUTDATA(ctypes.Union):
    _fields_ = [("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("data", _RAWINPUTDATA)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD),
                ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", W.DWORD), ("u", _INPUTUNION)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", W.DWORD), ("scanCode", W.DWORD), ("flags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, W.WPARAM, W.LPARAM)


def _fn(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype, f.argtypes = restype, argtypes
    return f


GetRawInputData = _fn(user32, "GetRawInputData", W.UINT, W.HANDLE, W.UINT, W.LPVOID,
                      ctypes.POINTER(W.UINT), W.UINT)
RegisterRawInputDevices = _fn(user32, "RegisterRawInputDevices", W.BOOL,
                              ctypes.POINTER(RAWINPUTDEVICE), W.UINT, W.UINT)
CreateWindowExW = _fn(user32, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
DestroyWindow = _fn(user32, "DestroyWindow", W.BOOL, W.HWND)
PeekMessageW = _fn(user32, "PeekMessageW", W.BOOL, ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT)
TranslateMessage = _fn(user32, "TranslateMessage", W.BOOL, ctypes.POINTER(W.MSG))
DispatchMessageW = _fn(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(W.MSG))
MsgWaitForMultipleObjects = _fn(user32, "MsgWaitForMultipleObjects", W.DWORD,
                                W.DWORD, W.LPVOID, W.BOOL, W.DWORD, W.DWORD)
SendInput = _fn(user32, "SendInput", W.UINT, W.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
SetWindowsHookExW = _fn(user32, "SetWindowsHookExW", W.HHOOK, ctypes.c_int, HOOKPROC, W.HINSTANCE, W.DWORD)
CallNextHookEx = _fn(user32, "CallNextHookEx", LRESULT, W.HHOOK, ctypes.c_int, W.WPARAM, W.LPARAM)
UnhookWindowsHookEx = _fn(user32, "UnhookWindowsHookEx", W.BOOL, W.HHOOK)
GetCursorPos = _fn(user32, "GetCursorPos", W.BOOL, ctypes.POINTER(W.POINT))
SetCursorPos = _fn(user32, "SetCursorPos", W.BOOL, ctypes.c_int, ctypes.c_int)
GetForegroundWindow = _fn(user32, "GetForegroundWindow", W.HWND)
GetWindowTextW = _fn(user32, "GetWindowTextW", ctypes.c_int, W.HWND, W.LPWSTR, ctypes.c_int)
GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", W.DWORD, W.HWND, ctypes.POINTER(W.DWORD))
GetKeyNameTextW = _fn(user32, "GetKeyNameTextW", ctypes.c_int, W.LONG, W.LPWSTR, ctypes.c_int)
MapVirtualKeyW = _fn(user32, "MapVirtualKeyW", W.UINT, W.UINT, W.UINT)
GetModuleHandleW = _fn(kernel32, "GetModuleHandleW", W.HMODULE, W.LPCWSTR)
OpenProcess = _fn(kernel32, "OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
CloseHandle = _fn(kernel32, "CloseHandle", W.BOOL, W.HANDLE)
QueryFullProcessImageNameW = _fn(kernel32, "QueryFullProcessImageNameW", W.BOOL,
                                 W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD))
K32EnumProcesses = _fn(kernel32, "K32EnumProcesses", W.BOOL,
                       ctypes.POINTER(W.DWORD), W.DWORD, ctypes.POINTER(W.DWORD))
OpenProcessToken = _fn(advapi32, "OpenProcessToken", W.BOOL, W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE))
GetTokenInformation = _fn(advapi32, "GetTokenInformation", W.BOOL,
                          W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD, ctypes.POINTER(W.DWORD))
IsUserAnAdmin = _fn(shell32, "IsUserAnAdmin", W.BOOL)
ShellExecuteW = _fn(shell32, "ShellExecuteW", W.HINSTANCE,
                    W.HWND, W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, ctypes.c_int)
timeBeginPeriod = _fn(winmm, "timeBeginPeriod", W.UINT, W.UINT)
timeEndPeriod = _fn(winmm, "timeEndPeriod", W.UINT, W.UINT)


# --------------------------- Utilitaires -----------------------------

def is_admin():
    try:
        return bool(IsUserAnAdmin())
    except OSError:
        return False


def process_name(pid):
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = W.DWORD(len(buf))
        if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return None
    finally:
        CloseHandle(h)


def process_elevated(pid):
    """True / False, ou None si on ne peut pas savoir (souvent : processus admin vu sans droits admin)."""
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        tok = W.HANDLE()
        if not OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            elev, ret = W.DWORD(), W.DWORD()
            if not GetTokenInformation(tok, TOKEN_ELEVATION_CLASS, ctypes.byref(elev), 4, ctypes.byref(ret)):
                return None
            return bool(elev.value)
        finally:
            CloseHandle(tok)
    finally:
        CloseHandle(h)


def find_roblox():
    pids = (W.DWORD * 8192)()
    needed = W.DWORD()
    if not K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    found = []
    for pid in pids[:needed.value // ctypes.sizeof(W.DWORD)]:
        name = process_name(pid)
        if name and name.lower() in ROBLOX_EXES:
            found.append((name, pid, process_elevated(pid)))
    return found


def key_name(scan, ext):
    buf = ctypes.create_unicode_buffer(64)
    if GetKeyNameTextW((scan << 16) | (ext << 24), buf, len(buf)):
        return buf.value
    return "scan 0x%02X%s" % (scan, " E0" if ext else "")


_logger = print


def set_logger(fn):
    """Redirige les messages (l'interface graphique les affiche dans sa fenetre)."""
    global _logger
    _logger = fn


def log(msg):
    _logger(msg)


def relaunch_as_admin(script, args):
    if getattr(sys, "frozen", False):  # version .exe : on relance l'exe lui-meme
        params, folder = subprocess.list2cmdline(list(args)), os.path.dirname(sys.executable)
    else:
        params, folder = subprocess.list2cmdline([script] + list(args)), os.path.dirname(script)
    r = ShellExecuteW(None, "runas", sys.executable, params, folder, 1)
    ok = (r or 0) > 32
    log("Relance en administrateur dans une nouvelle fenetre." if ok else "Elevation annulee ou impossible.")
    return ok


def cursor_pos():
    pt = W.POINT()
    return [pt.x, pt.y] if GetCursorPos(ctypes.byref(pt)) else None


def save_macro(path, events, duration, start_pos, meta=None):
    header = {"format": "roblox-macro-1", "duration": round(duration, 6), "start_pos": start_pos}
    header.update(meta or {})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        # En-tete sur la premiere ligne (lisible sans charger tous les evenements).
        f.write(json.dumps(header, ensure_ascii=False)[:-1] + ',\n "events": [\n')
        f.write(",\n".join(json.dumps(e) for e in events))
        f.write("\n]}\n")
    os.replace(tmp, path)


def load_macro(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    events = data["events"]
    duration = float(data.get("duration") or (events[-1][0] if events else 0.0))
    return events, duration, data.get("start_pos")


def describe(events, duration):
    dx = sum(e[2] for e in events if e[1] == "m")
    dy = sum(e[3] for e in events if e[1] == "m")
    keys = sum(1 for e in events if e[1] == "k" and e[4])
    return "%d evts, %.2f s, %d touches, souris total dx=%+d dy=%+d" % (len(events), duration, keys, dx, dy)


# ----------------------------- Lecture -------------------------------

def _mouse_input(dx=0, dy=0, flags=MOUSEEVENTF_MOVE, data=0):
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, INJECT_TAG)
    return inp


def _key_input(scan, ext, down):
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if ext else 0) | (0 if down else KEYEVENTF_KEYUP)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(0, scan, flags, 0, INJECT_TAG)
    return inp


def send_inputs(batch):
    """Envoie une liste d'INPUT ; renvoie True si Windows les a tous acceptes."""
    if not batch:
        return True
    arr = (INPUT * len(batch))(*batch)
    return SendInput(len(batch), arr, ctypes.sizeof(INPUT)) == len(batch)


def click_at(x, y, button="left"):
    """Place la souris en (x, y) (pixels ecran) et clique."""
    SetCursorPos(int(x), int(y))
    send_inputs([_mouse_input(1, 0), _mouse_input(-1, 0)])  # le jeu voit le survol avant le clic
    time.sleep(0.03)
    down, data = SEND_BUTTONS[(button, 1)]
    up, _ = SEND_BUTTONS[(button, 0)]
    send_inputs([_mouse_input(0, 0, down, data)])
    time.sleep(0.04)
    send_inputs([_mouse_input(0, 0, up, data)])


class Player(threading.Thread):
    def __init__(self, events, duration, start_pos, repeat, cfg, name="", on_state=None):
        super().__init__(daemon=True)
        self.events, self.duration, self.start_pos, self.cfg = events, duration, start_pos, cfg
        self.repeat, self.name = repeat, name
        self.on_state = on_state or (lambda state, text: None)
        self.stop_evt = threading.Event()
        self.held_keys, self.held_btns = set(), set()
        self.sent_dx = self.sent_dy = 0
        self.warned = False

    def stop(self):
        self.stop_evt.set()

    def _wait_until(self, target):
        # Attente passive jusqu'a ~2 ms avant l'echeance, puis attente active pour la precision.
        while not self.stop_evt.is_set():
            rem = target - time.perf_counter()
            if rem <= 0:
                return
            if rem > 0.003:
                self.stop_evt.wait(rem - 0.002)
            else:
                time.sleep(0)

    def _send(self, batch):
        if not send_inputs(batch) and not self.warned:
            self.warned = True
            log("[!] SendInput refuse (erreur %d). Roblox en admin ? Essaie --admin." % ctypes.get_last_error())

    def _move(self, batch, fx, fy):
        # On garde la partie fractionnaire pour ne rien perdre avec SENSITIVITY != 1.
        ix, iy = round(fx), round(fy)
        if ix or iy:
            batch.append(_mouse_input(ix, iy))
            self.sent_dx += ix
            self.sent_dy += iy
        return fx - ix, fy - iy

    def _event(self, batch, ev):
        kind = ev[1]
        if kind == "k":
            _, _, scan, ext, down = ev
            batch.append(_key_input(scan, ext, down))
            (self.held_keys.add if down else self.held_keys.discard)((scan, ext))
        elif kind == "b":
            _, _, name, down = ev
            flag, data = SEND_BUTTONS[(name, down)]
            batch.append(_mouse_input(0, 0, flag, data))
            (self.held_btns.add if down else self.held_btns.discard)(name)
        elif kind == "w":
            batch.append(_mouse_input(0, 0, MOUSEEVENTF_WHEEL, ev[2]))
        elif kind == "hw":
            batch.append(_mouse_input(0, 0, MOUSEEVENTF_HWHEEL, ev[2]))

    def _release_all(self):
        batch = [_key_input(scan, ext, 0) for scan, ext in self.held_keys]
        batch += [_mouse_input(0, 0, *SEND_BUTTONS[(name, 0)]) for name in self.held_btns]
        self._send(batch)
        self.held_keys.clear()
        self.held_btns.clear()

    def run(self):
        cfg, evs = self.cfg, self.events
        label = "%s " % self.name if self.name else ""
        n = 0
        try:
            while not self.stop_evt.is_set() and (self.repeat == 0 or n < self.repeat):
                if n and cfg.loop_delay and self.stop_evt.wait(cfg.loop_delay):
                    break
                n += 1
                progress = "%s%d/%s" % (label, n, self.repeat or "infini")
                log("[PLAY] Lecture " + progress)
                self.on_state("play", "Lecture " + progress)
                if cfg.restore_pos and self.start_pos:
                    SetCursorPos(*self.start_pos)
                start = time.perf_counter()
                i, rx, ry = 0, 0.0, 0.0
                while i < len(evs):
                    self._wait_until(start + evs[i][0] / cfg.speed)
                    if self.stop_evt.is_set():
                        break
                    # Envoie tout ce qui est echu ; les mouvements consecutifs sont additionnes.
                    now = (time.perf_counter() - start) * cfg.speed
                    batch, mx, my = [], 0.0, 0.0
                    while i < len(evs) and evs[i][0] <= now:
                        ev = evs[i]
                        i += 1
                        if ev[1] == "m":
                            mx += ev[2] * cfg.sens
                            my += ev[3] * cfg.sens
                            continue
                        if mx or my:
                            rx, ry = self._move(batch, mx + rx, my + ry)
                            mx = my = 0.0
                        self._event(batch, ev)
                    if mx or my:
                        rx, ry = self._move(batch, mx + rx, my + ry)
                    self._send(batch)
                self._release_all()
                self._wait_until(start + self.duration / cfg.speed)
        finally:
            self._release_all()
            state = "arretee" if self.stop_evt.is_set() else "terminee"
            log("[STOP] Lecture %s. Souris envoyee : dx=%+d dy=%+d" % (state, self.sent_dx, self.sent_dy))
            self.on_state("idle", "Pret")


# ------------------------ Capture + raccourcis ------------------------

class Engine:
    """Capture Raw Input, raccourcis clavier et lecture.

    run() pompe les messages Windows dans son thread ; l'etat (enregistrement, lecture)
    ne se modifie que dans ce thread. Depuis un autre thread, passer par call().
    """

    def __init__(self, cfg, admin):
        self.cfg, self.admin = cfg, admin
        self.hotkeys = set(HOTKEYS)
        self.events, self.duration, self.start_pos = [], 0.0, None
        self.recording = False
        self.t0 = 0.0
        self.rec_keys, self.rec_btns = set(), set()
        self.keys_down = set()   # touches physiques enfoncees (filtre l'auto-repetition)
        self.hot_down = set()
        self.pending = queue.Queue()  # (fonction, args) a executer dans le thread de run()
        self.running = False
        self.player = None
        self.dbg = {"souris ": [0, 0, 0], "injecte": [0, 0, 0]}
        self.dbg_last = 0.0
        self.abs_warned = False
        self.rawbuf = ctypes.create_string_buffer(ctypes.sizeof(RAWINPUT) + 64)
        self.raw = RAWINPUT.from_buffer(self.rawbuf)
        self._hook_cb = HOOKPROC(self._on_hook)  # garder une reference, sinon crash

    # -- a surcharger --

    def on_hotkey(self, vk):
        """Raccourcis autres que F8 (F8 est gere directement ici)."""

    def on_recorded(self, events, duration, start_pos):
        """Appele a la fin d'un enregistrement non vide."""

    def on_state(self, state, text):
        """state : "idle", "rec" ou "play". Peut etre appele depuis le thread de lecture."""

    # -- API utilisable depuis n'importe quel thread --

    def call(self, fn, *args):
        self.pending.put((fn, args))

    def quit(self):
        self.running = False

    def playing(self):
        return self.player is not None and self.player.is_alive()

    def busy(self):
        return self.recording or self.playing()

    # -- raccourcis (hook clavier bas niveau : peut "avaler" les touches) --

    def _on_hook(self, code, wparam, lparam):
        try:
            if code == 0:
                kb = KBDLLHOOKSTRUCT.from_address(lparam)
                if kb.vkCode in self.hotkeys:
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if kb.vkCode not in self.hot_down:
                            self.hot_down.add(kb.vkCode)
                            self.call(self._hotkey, kb.vkCode)
                    else:
                        self.hot_down.discard(kb.vkCode)
                    if self.cfg.swallow:
                        return 1
        except Exception:
            log(traceback.format_exc())
        return CallNextHookEx(None, code, wparam, lparam)

    def _hotkey(self, vk):
        if vk == HOTKEY_RECORD:
            self.toggle_record()
        else:
            self.on_hotkey(vk)

    # -- Raw Input --

    def _on_raw(self, hraw):
        size = W.UINT(ctypes.sizeof(self.rawbuf))
        got = GetRawInputData(hraw, RID_INPUT, self.rawbuf, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        if got in (0, 0xFFFFFFFF):
            return
        now = time.perf_counter()
        if self.raw.header.dwType == RIM_TYPEMOUSE:
            self._on_mouse(self.raw.data.mouse, now)
        elif self.raw.header.dwType == RIM_TYPEKEYBOARD:
            self._on_key(self.raw.data.keyboard, now)

    def _on_mouse(self, m, now):
        ours = m.ulExtraInformation == INJECT_TAG
        dx, dy = m.lLastX, m.lLastY
        if m.usFlags & MOUSE_MOVE_ABSOLUTE:
            if (dx or dy) and not self.abs_warned:
                self.abs_warned = True
                log("[!] Souris en coordonnees absolues (bureau a distance, VM, tablette ?) : mouvements ignores.")
            dx = dy = 0
        rec = self.recording and not ours
        t = round(now - self.t0, 6)
        if dx or dy:
            if self.cfg.debug:
                d = self.dbg["injecte" if ours else "souris "]
                d[0] += dx
                d[1] += dy
                d[2] += 1
            if rec:
                self.events.append([t, "m", dx, dy])
        flags = m.u.b.usButtonFlags
        if not flags:
            return
        for flag, name, down in RAW_BUTTONS:
            if flags & flag:
                if self.cfg.debug:
                    self._dbg_print("bouton %s %s" % (BUTTON_FR[name], "bas" if down else "haut"), ours)
                if rec:
                    self.events.append([t, "b", name, down])
                    (self.rec_btns.add if down else self.rec_btns.discard)(name)
        for flag, kind in ((RI_MOUSE_WHEEL, "w"), (RI_MOUSE_HWHEEL, "hw")):
            if flags & flag:
                delta = ctypes.c_short(m.u.b.usButtonData).value
                if self.cfg.debug:
                    self._dbg_print("molette%s %+d" % (" horiz." if kind == "hw" else "", delta), ours)
                if rec:
                    self.events.append([t, kind, delta])

    def _on_key(self, k, now):
        ours = k.ExtraInformation == INJECT_TAG
        vk, scan, fl = k.VKey, k.MakeCode, k.Flags
        if vk in self.hotkeys or vk == 0xFF or scan == 0xFF or fl & RI_KEY_E1:
            return  # raccourcis, touches factices, Pause
        ext = 1 if fl & RI_KEY_E0 else 0
        if ext and scan in (0x2A, 0x36):
            return  # faux Shift ajoutes par Windows autour des fleches/pave num.
        if scan == 0:
            scan = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
            if scan == 0:
                return
        down = 0 if fl & RI_KEY_BREAK else 1
        key = (scan, ext)
        if not ours:
            if down and key in self.keys_down:
                return  # auto-repetition
            (self.keys_down.add if down else self.keys_down.discard)(key)
        if self.cfg.debug:
            self._dbg_print("touche %s %s" % (key_name(scan, ext), "bas" if down else "haut"), ours)
        if self.recording and not ours:
            self.events.append([round(now - self.t0, 6), "k", scan, ext, down])
            (self.rec_keys.add if down else self.rec_keys.discard)(key)

    # -- debug --

    def _dbg_flush(self, force=False):
        now = time.perf_counter()
        if not force and now - self.dbg_last < self.cfg.debug_interval:
            return
        self.dbg_last = now
        for label, d in self.dbg.items():
            if d[2]:
                log("[debug] %s dx=%+6d dy=%+6d  (%d msg)" % (label, d[0], d[1], d[2]))
                d[0] = d[1] = d[2] = 0

    def _dbg_print(self, text, ours):
        self._dbg_flush(force=True)
        log("[debug] %s%s" % (text, " (injecte)" if ours else ""))

    # -- actions (thread de run() uniquement) --

    def toggle_record(self):
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if self.recording:
            return
        if self.playing():
            log("Lecture en cours : arrete-la d'abord.")
            return
        self.events = []
        self.rec_keys.clear()
        self.rec_btns.clear()
        self.start_pos = cursor_pos()
        self.t0 = time.perf_counter()
        self.recording = True
        log("[REC] Enregistrement... (F8 pour arreter) - souris en %s" % (self.start_pos,))
        self.on_state("rec", "Enregistrement (F8 pour arreter)")

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        t = round(time.perf_counter() - self.t0, 6)
        # Relache ce qui est encore enfonce, pour que la lecture ne laisse rien bloque.
        for scan, ext in self.rec_keys:
            self.events.append([t, "k", scan, ext, 0])
        for name in self.rec_btns:
            self.events.append([t, "b", name, 0])
        self.duration = t
        self.on_state("idle", "Pret")
        if not self.events:
            log("[REC] Rien enregistre.")
            return
        log("[REC] Termine : %s" % describe(self.events, self.duration))
        self.on_recorded(self.events, self.duration, self.start_pos)

    def play(self, events, duration, start_pos, repeat, name=""):
        if self.recording:
            log("Enregistrement en cours : F8 pour l'arreter d'abord.")
            return False
        if self.playing():
            log("Une lecture est deja en cours.")
            return False
        if not events:
            log("Macro vide.")
            return False
        hwnd = GetForegroundWindow()
        title = ctypes.create_unicode_buffer(256)
        GetWindowTextW(hwnd, title, len(title))
        pid = W.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        log("[PLAY] Fenetre active : %s" % (title.value or "?"))
        if not self.admin and process_elevated(pid.value):
            log("[!] Cette fenetre tourne en admin : Windows bloquera la lecture. Relance en admin.")
        if repeat == 0:
            log("[PLAY] Boucle infinie : F9 pour arreter.")
        self.player = Player(events, duration, start_pos, repeat, self.cfg, name, self.on_state)
        self.player.start()
        return True

    def stop_playback(self):
        if self.playing():
            self.player.stop()

    # -- boucle principale --

    def run(self):
        hinst = GetModuleHandleW(None)
        # Fenetre invisible qui recoit WM_INPUT, meme quand Roblox a le focus (RIDEV_INPUTSINK).
        hwnd = CreateWindowExW(0, "STATIC", "roblox_macro", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        devices = (RAWINPUTDEVICE * 2)(
            RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd),  # souris
            RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd),  # clavier
        )
        if not RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE)):
            raise ctypes.WinError(ctypes.get_last_error())
        hook = SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_cb, hinst, 0)
        if not hook:
            raise ctypes.WinError(ctypes.get_last_error())
        timeBeginPeriod(1)
        msg = W.MSG()
        self.running = True
        try:
            while self.running:
                MsgWaitForMultipleObjects(0, None, False, 50, QS_ALLINPUT)
                while PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_INPUT:
                        self._on_raw(msg.lParam & PTR_MASK)
                    TranslateMessage(ctypes.byref(msg))
                    DispatchMessageW(ctypes.byref(msg))
                while not self.pending.empty():
                    fn, args = self.pending.get_nowait()
                    try:
                        fn(*args)
                    except Exception:
                        log(traceback.format_exc())
                if self.cfg.debug:
                    self._dbg_flush()
        finally:
            if self.playing():
                self.player.stop()
                self.player.join(2)
            if self.recording:
                self.stop_recording()  # sauvegarde ce qui a ete capture
            UnhookWindowsHookEx(hook)
            timeEndPeriod(1)
            DestroyWindow(hwnd)


class ConsoleApp(Engine):
    """Mode console : une seule macro, dans cfg.file."""

    def __init__(self, cfg, admin):
        super().__init__(cfg, admin)
        self.macro = None  # (events, duration, start_pos)

    def on_recorded(self, events, duration, start_pos):
        self.macro = (events, duration, start_pos)
        save_macro(self.cfg.file, events, duration, start_pos)
        log("      Sauve dans %s" % self.cfg.file)

    def on_hotkey(self, vk):
        if vk not in (HOTKEY_PLAY, HOTKEY_LOOP):
            return
        if self.playing():
            self.stop_playback()
            return
        if self.recording:
            log("Enregistrement en cours : F8 pour l'arreter d'abord.")
            return
        if self.macro is None:
            try:
                self.macro = load_macro(self.cfg.file)
            except FileNotFoundError:
                log("Aucune macro : enregistre d'abord avec F8.")
                return
            except (ValueError, KeyError, IndexError) as e:
                log("Fichier macro illisible (%s)." % e)
                return
        self.play(*self.macro, 0 if vk == HOTKEY_LOOP else self.cfg.repeat)


# ------------------------------- Main --------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Enregistreur/lecteur de macros Roblox (Raw Input, Shift Lock).")
    p.add_argument("--file", default=MACRO_FILE, help="fichier de macro (JSON)")
    p.add_argument("--speed", type=float, default=SPEED, help="facteur de vitesse")
    p.add_argument("--sens", type=float, default=SENSITIVITY, help="multiplicateur des deltas souris")
    p.add_argument("--repeat", type=int, default=REPEAT, help="nombre de lectures, 0 = infini")
    p.add_argument("--loop-delay", type=float, default=LOOP_DELAY, help="pause entre deux lectures (s)")
    p.add_argument("--debug", action="store_true", default=DEBUG, help="affiche les deltas captures")
    p.add_argument("--admin", action="store_true", help="se relance en administrateur")
    p.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    cfg = p.parse_args()
    if cfg.speed <= 0:
        p.error("--speed doit etre > 0")
    if cfg.repeat < 0:
        p.error("--repeat doit etre >= 0")
    if not os.path.isabs(cfg.file):
        cfg.file = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg.file)
    cfg.swallow = SWALLOW_HOTKEYS
    cfg.restore_pos = RESTORE_START_POS
    cfg.debug_interval = DEBUG_INTERVAL
    return cfg


def report_roblox(admin):
    roblox = find_roblox()
    if not roblox:
        log("Roblox pas detecte (pas encore lance ?).")
    for name, pid, elev in roblox:
        state = {True: "oui", False: "non", None: "inconnu"}[elev]
        log("Roblox detecte : %s (admin : %s)" % (name, state))
        if not admin and elev:
            log("[!] Roblox tourne en admin : relance ce programme en administrateur.")
        elif not admin and elev is None:
            log("    Si Roblox a ete lance en admin, relance ce programme en administrateur.")


def main():
    if sys.platform != "win32":
        sys.exit("Windows uniquement.")
    cfg = parse_args()
    admin = is_admin()
    if cfg.admin and not admin:
        relaunch_as_admin(os.path.abspath(__file__), sys.argv[1:] + ["--pause"])
        return
    try:
        print("=== Macro Roblox (Raw Input) ===")
        print("Fichier : %s" % cfg.file)
        print("Vitesse x%g | Sensibilite x%g | Repetitions %s | Debug %s"
              % (cfg.speed, cfg.sens, cfg.repeat or "infini", "oui" if cfg.debug else "non"))
        print("Droits admin : %s" % ("oui" if admin else "non"))
        report_roblox(admin)
        app = ConsoleApp(cfg, admin)
        if os.path.exists(cfg.file):
            try:
                app.macro = load_macro(cfg.file)
                print("Macro chargee : %s" % describe(app.macro[0], app.macro[1]))
            except (ValueError, KeyError, IndexError) as e:
                print("Fichier macro illisible, ignore (%s)." % e)
        print("Pret. F8 = enregistrer, F9 = lire, F10 = lire en boucle, Ctrl+C = quitter.")
        try:
            app.run()
        except KeyboardInterrupt:
            print("\nFermeture.")
    except Exception:
        traceback.print_exc()
        if not cfg.pause:
            raise SystemExit(1)
    finally:
        if cfg.pause:
            input("Entree pour fermer...")


if __name__ == "__main__":
    main()
