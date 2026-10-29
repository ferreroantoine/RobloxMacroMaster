# -*- coding: utf-8 -*-
"""
macro_gui.py - interface pour roblox_macro.py (a garder dans le meme dossier).

- Bibliotheque de macros nommees (dossier macros/), a rejouer quand on veut.
- Scenarios : suites d'etapes executees dans l'ordre (cliquer sur telle image,
  puis attendre telle autre, jouer une macro...). Chaque etape ne cherche que
  SON image : l'ordre est respecte meme si plusieurs images sont visibles.
- Declencheurs par image : si une image apparait a l'ecran, on clique dessus
  et/ou on lance une macro ou un scenario.
- Export / import de toute la configuration (menu Fichier) dans un .zip.

Dependances pour les images :
    pip install opencv-python numpy mss pillow
Sans elles, l'interface fonctionne mais tout ce qui touche aux images est desactive.

Raccourcis (meme quand Roblox a le focus) :
  F8   enregistrer / arreter (la macro est ajoutee a la bibliotheque)
  F9   si quelque chose tourne : l'arreter. Sinon, selon la page ouverte :
         Actif -> surveillance on/off, Macros -> jouer la macro selectionnee,
         Scenarios -> lancer le scenario selectionne, Images -> tester l'image selectionnee
"""

import copy
import ctypes
import json
import math
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import zipfile
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from types import SimpleNamespace

# Coordonnees en vrais pixels : capture, recherche d'image et clics restent alignes
# meme avec une mise a l'echelle Windows de 125 % / 150 %.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import roblox_macro as rm

try:
    import cv2
    import mss
    import numpy as np
    from PIL import Image, ImageTk
    import mission_stats as ms
    from mission_stats import load_template, match_image
    VISION_ERROR = None
except ImportError as e:
    VISION_ERROR = str(e)

# Version .exe : les donnees vont a cote de l'exe, pas dans le dossier temporaire de PyInstaller.
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
MACRO_DIR = os.path.join(BASE, "macros")
IMAGE_DIR = os.path.join(BASE, "images")
SETTINGS_FILE = os.path.join(BASE, "settings.json")
TRIGGERS_FILE = os.path.join(BASE, "triggers.json")
SCENARIOS_FILE = os.path.join(BASE, "scenarios.json")
STATS_DIR = os.path.join(BASE, "stats")
SESSIONS_CSV = os.path.join(STATS_DIR, "sessions.csv")
CURRENT_SESSION = os.path.join(STATS_DIR, "session_en_cours.json")

DEFAULT_SETTINGS = {
    "speed": rm.SPEED, "sens": rm.SENSITIVITY, "repeat": rm.REPEAT, "loop_delay": rm.LOOP_DELAY,
    "restore_pos": rm.RESTORE_START_POS, "debug": False, "countdown": 3.0,
    "watch_interval": 0.5, "topmost": False,
    "hud": True, "hud_corner": "haut-gauche", "hud_alpha": 0.8,
}
HUD_CORNERS = ["haut-gauche", "haut-droite", "bas-gauche", "bas-droite"]
DEFAULT_TRIGGER = {
    "name": "", "image": "", "macro": "", "scenario": "", "click": True,
    "threshold": 0.85, "cooldown": 3.0, "enabled": True,
}
DEFAULT_STEP = {
    "type": "click", "image": "", "threshold": 0.85, "timeout": 10.0, "button": "left",
    "macro": "", "seconds": 1.0, "after": 0.5, "on_fail": "stop", "goto": 1,
}
NO_MACRO = "(aucune - clic seulement)"

STEP_TYPES = [
    ("click", "Cliquer sur une image"),
    ("wait_image", "Attendre qu'une image apparaisse"),
    ("wait_gone", "Attendre qu'une image disparaisse"),
    ("macro", "Jouer une macro"),
    ("wait", "Pause"),
]
IMAGE_STEPS = ("click", "wait_image", "wait_gone")
FAIL_ACTIONS = [
    ("stop", "Arreter le scenario"),
    ("next", "Passer a l'etape suivante"),
    ("restart", "Recommencer le scenario"),
    ("goto", "Aller a l'etape numero"),
]
BUTTONS = [("left", "Clic gauche"), ("right", "Clic droit"), ("double", "Double clic")]


# ----------------------------- Fichiers ------------------------------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        rm.log("[!] %s illisible, ignore." % path)
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def num(text):
    return float(str(text).replace(",", "."))


def safe_name(name):
    name = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)
    return name.strip().rstrip(".")[:80]


def macro_path(name):
    return os.path.join(MACRO_DIR, name + ".json")


def unique_name(name):
    base, n = name, 2
    while os.path.exists(macro_path(name)):
        name = "%s (%d)" % (base, n)
        n += 1
    return name


def macro_names():
    return sorted((f[:-5] for f in os.listdir(MACRO_DIR) if f.lower().endswith(".json")), key=str.lower)


def macro_info(name):
    """(duree, date de creation) sans charger tous les evenements."""
    path = macro_path(name)
    try:
        with open(path, encoding="utf-8") as f:
            header = json.loads(f.readline().strip().rstrip(",") + "}")
    except (OSError, ValueError):
        header = {}
    created = header.get("created") or time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    return header.get("duration"), created


def rel_image(path):
    path = os.path.abspath(path)
    return os.path.relpath(path, BASE) if path.startswith(BASE + os.sep) else path


def abs_image(path):
    return path if os.path.isabs(path) else os.path.join(BASE, path)


def new_image_path(stem, ext=".png"):
    os.makedirs(IMAGE_DIR, exist_ok=True)
    path = os.path.join(IMAGE_DIR, stem + ext)
    n = 2
    while os.path.exists(path):
        path = os.path.join(IMAGE_DIR, "%s_%d%s" % (stem, n, ext))
        n += 1
    return path


def normalize_trigger(t):
    return dict(DEFAULT_TRIGGER, **t)


def normalize_scenario(s):
    return {"name": str(s.get("name") or "Scenario"), "repeat": max(0, int(s.get("repeat", 1))),
            "steps": [dict(DEFAULT_STEP, **step) for step in s.get("steps", [])]}


def image_objects(triggers, scenarios):
    """Tous les dicts (declencheurs, etapes) qui referencent une image."""
    for t in triggers:
        if t.get("image"):
            yield t
    for s in scenarios:
        for step in s["steps"]:
            if step["type"] in IMAGE_STEPS and step.get("image"):
                yield step


def merge_by_name(old, new):
    """Ajoute `new` a `old` ; un element du meme nom est remplace a sa place."""
    out = list(old)
    index = {x["name"]: i for i, x in enumerate(out)}
    for x in new:
        if x["name"] in index:
            out[index[x["name"]]] = x
        else:
            index[x["name"]] = len(out)
            out.append(x)
    return out


def action_label(t):
    if t["scenario"]:
        return "Scenario : " + t["scenario"]
    if t["macro"]:
        return "Macro : " + t["macro"]
    return "-"


def step_text(step):
    """(action, detail, si echec) pour la liste des etapes."""
    t = step["type"]
    if t in IMAGE_STEPS:
        detail = "%s (max %g s, ress. %.2f)" % (os.path.basename(step["image"]) or "?",
                                                step["timeout"], step["threshold"])
        if t == "click" and step["button"] != "left":
            detail = "%s - %s" % (dict(BUTTONS)[step["button"]], detail)
    elif t == "macro":
        duration = macro_info(step["macro"])[0] if os.path.exists(macro_path(step["macro"])) else None
        detail = "%s (%s, attend la fin)" % (step["macro"] or "?",
                                             "%.1f s" % duration if duration is not None else "introuvable")
    else:
        detail = "%g s" % step["seconds"]
    if t != "wait" and step["after"]:
        detail += ", puis %g s" % step["after"]
    if t == "wait":
        fail = "-"
    elif step["on_fail"] == "goto":
        fail = "Aller a l'etape %d" % step["goto"]
    else:
        fail = dict(FAIL_ACTIONS)[step["on_fail"]]
    return dict(STEP_TYPES)[t], detail, fail


def export_config(path, triggers, scenarios, settings):
    """Ecrit macros, images, declencheurs, scenarios et reglages dans un .zip."""
    triggers, scenarios = copy.deepcopy(triggers), copy.deepcopy(scenarios)
    names = macro_names()
    added = {}  # chemin de l'image -> nom dans le zip
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            z.write(macro_path(name), "macros/%s.json" % name)
        for obj in image_objects(triggers, scenarios):
            src = abs_image(obj["image"])
            if not os.path.exists(src):
                continue
            key = os.path.normcase(os.path.abspath(src))
            if key not in added:
                stem, ext = os.path.splitext(os.path.basename(src))
                arc, n = "images/%s%s" % (stem, ext), 2
                while arc in added.values():
                    arc = "images/%s_%d%s" % (stem, n, ext)
                    n += 1
                z.write(src, arc)
                added[key] = arc
            obj["image"] = added[key]
        z.writestr("triggers.json", json.dumps(triggers, ensure_ascii=False, indent=1))
        z.writestr("scenarios.json", json.dumps(scenarios, ensure_ascii=False, indent=1))
        z.writestr("settings.json", json.dumps(settings, ensure_ascii=False, indent=1))
        z.writestr("manifest.json", json.dumps({"app": "macro-roblox", "version": 1,
                                                "exported": time.strftime("%Y-%m-%d %H:%M")}))
    return len(names), len(added)


# ------------------------- Recherche d'image --------------------------

def screen_grabber():
    return (getattr(mss, "MSS", None) or mss.mss)()


def primary_monitor(sct):
    """L'ecran principal est celui qui contient (0, 0) ; monitors[1] n'est pas forcement lui."""
    for mon in sct.monitors[1:]:
        if mon["left"] <= 0 < mon["left"] + mon["width"] and mon["top"] <= 0 < mon["top"] + mon["height"]:
            return mon
    return sct.monitors[1]


def grab_screen(sct):
    """Capture de l'ecran principal en BGR + son coin haut-gauche."""
    mon = primary_monitor(sct)
    shot = np.asarray(sct.grab(mon))[:, :, :3]
    return np.ascontiguousarray(shot), (mon["left"], mon["top"])


class TemplateCache:
    """Images chargees une seule fois, rechargees si le fichier change."""

    def __init__(self):
        self.items = {}

    def get(self, path):
        mtime = os.path.getmtime(path)
        cached = self.items.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        img = load_template(path)
        self.items[path] = (mtime, img)
        return img


class Watcher(threading.Thread):
    """Cherche periodiquement les images des declencheurs actifs sur l'ecran principal."""

    def __init__(self, gui):
        super().__init__(daemon=True)
        self.gui = gui
        self.stop_evt = threading.Event()
        self.cache = TemplateCache()
        self.next_ok = {}     # nom du declencheur -> instant ou il peut se redeclencher
        self.reported = set()
        self.paused = False   # rien n'est cherche pendant une lecture / un scenario
        self.scores = {}      # nom du declencheur -> derniere ressemblance mesuree (page Actif)

    def stop(self):
        self.stop_evt.set()

    def run(self):
        try:
            with screen_grabber() as sct:
                while not self.stop_evt.is_set():
                    self.paused = self.gui.engine.busy() or self.gui.countdown_id is not None
                    if not self.paused:
                        self._scan(sct)
                    self.stop_evt.wait(max(0.05, self.gui.cfg.watch_interval))
        except Exception:
            rm.log("[!] Surveillance arretee :\n" + traceback.format_exc())
            self.gui.post("watch_stopped")

    def _scan(self, sct):
        triggers = [t for t in self.gui.triggers if t.get("enabled")]
        if not triggers:
            return
        screen, (left, top) = grab_screen(sct)
        now = time.monotonic()
        for t in triggers:
            if now < self.next_ok.get(t["name"], 0):
                continue
            path = abs_image(t["image"])
            try:
                tmpl = self.cache.get(path)
            except (OSError, ValueError) as e:
                if path not in self.reported:
                    self.reported.add(path)
                    rm.log("[!] Image de '%s' inutilisable : %s" % (t["name"], e))
                continue
            score, center = match_image(screen, tmpl)
            self.scores[t["name"]] = score
            if center and score >= t["threshold"]:
                self.next_ok[t["name"]] = now + t["cooldown"]
                self.gui.post("trigger", t, (left + center[0], top + center[1]), score)
                return  # un declencheur a la fois


class MissionWatcher(threading.Thread):
    """Repere les ecrans de fin de mission. Jamais en pause : le scenario lance par l'ecran de fin
    (ex. "heal and retry") tourne justement pendant qu'il est affiche. Ne clique jamais."""

    def __init__(self, gui):
        super().__init__(daemon=True)
        self.gui = gui
        self.stop_evt = threading.Event()
        self.detector = ms.EndScreenDetector(gui.reader.read)

    def stop(self):
        self.stop_evt.set()

    def run(self):
        reported = set()
        try:
            with screen_grabber() as sct:
                while not self.stop_evt.is_set():
                    screen, _ = grab_screen(sct)
                    try:
                        result = self.detector.feed(screen, time.monotonic())
                    except Exception as e:
                        # Une image mal lue ne doit pas arreter le compteur pour toute la session.
                        result = None
                        if repr(e) not in reported:
                            reported.add(repr(e))
                            rm.log("[!] Lecture d'ecran de fin en echec (on continue) :\n" + traceback.format_exc())
                    if result is not None and not self.stop_evt.is_set():
                        self.gui.post("mission", result)
                    self.stop_evt.wait(0.3)
        except Exception:
            rm.log("[!] Compteur de missions arrete :\n" + traceback.format_exc())
            self.gui.post("mission_watch_stopped")


class ScenarioRunner(threading.Thread):
    """Execute les etapes d'un scenario dans l'ordre ; chaque etape ne cherche que son image."""

    def __init__(self, gui, scenario, pre_click=None):
        super().__init__(daemon=True)
        self.gui, self.scenario, self.pre_click = gui, scenario, pre_click
        self.stop_evt = threading.Event()
        self.player = None
        self.cache = TemplateCache()
        self.sct = None
        self.current = "Scenario %s" % scenario["name"]  # affiche dans la page Actif

    def stop(self):
        self.stop_evt.set()
        player = self.player
        if player:
            player.stop()

    def run(self):
        s = self.scenario
        try:
            if self.pre_click:
                rm.click_at(*self.pre_click)
                self.stop_evt.wait(0.1)
            with screen_grabber() as self.sct:
                n = 0
                while not self.stop_evt.is_set() and (s["repeat"] == 0 or n < s["repeat"]):
                    n += 1
                    rm.log("[SCENARIO] %s : tour %d/%s" % (s["name"], n, s["repeat"] or "infini"))
                    if not self._run_once(n):
                        break
        except Exception:
            rm.log("[!] Scenario interrompu :\n" + traceback.format_exc())
        finally:
            rm.log("[SCENARIO] %s %s." % (s["name"], "arrete" if self.stop_evt.is_set() else "termine"))
            self.gui.post("scenario_done")

    def _run_once(self, n):
        """Un tour complet ; False si le scenario doit s'arreter."""
        s = self.scenario
        steps = s["steps"]
        i = 0
        while i < len(steps):
            if self.stop_evt.is_set():
                return False
            step = steps[i]
            action = step_text(step)[0]
            if step["type"] == "macro":
                action = "macro %s" % step["macro"]
            self.current = "Scenario %s %d/%s - etape %d/%d : %s" % (
                s["name"], n, s["repeat"] or "inf", i + 1, len(steps), action)
            self.gui.post("state", "play", self.current)
            if self._do(step):
                if step["type"] != "wait" and step["after"]:
                    self.stop_evt.wait(step["after"])
                i += 1
                continue
            if self.stop_evt.is_set():
                return False
            fail = step["on_fail"]
            rm.log("[SCENARIO] Etape %d en echec -> %s" % (i + 1, step_text(step)[2]))
            if fail == "next":
                i += 1
            elif fail == "restart":
                i = 0
                self.stop_evt.wait(0.2)
            elif fail == "goto":
                i = min(max(int(step["goto"]) - 1, 0), len(steps) - 1)
                self.stop_evt.wait(0.1)
            else:
                return False
        return True

    def _do(self, step):
        """True si l'etape a reussi."""
        t = step["type"]
        if t == "wait":
            return not self.stop_evt.wait(step["seconds"])
        if t == "macro":
            macro = self.gui.load_named(step["macro"])
            if macro is None:
                return False
            rm.log("[SCENARIO] Macro '%s' (%.1f s) : on attend la fin avant l'etape suivante."
                   % (step["macro"], macro[1] / self.gui.cfg.speed))
            # Lecture dans ce thread : le scenario ne continue qu'une fois la macro finie.
            self.player = rm.Player(*macro, 1, self.gui.cfg, step["macro"], on_state=lambda state, text: None)
            self.player.run()
            self.player = None
            return not self.stop_evt.is_set()
        if VISION_ERROR:
            rm.log("[!] Etape image impossible : pip install opencv-python numpy mss pillow")
            return False
        try:
            tmpl = self.cache.get(abs_image(step["image"]))
        except (OSError, ValueError) as e:
            rm.log("[!] Image de l'etape inutilisable : %s" % e)
            return False
        deadline = time.monotonic() + step["timeout"]
        while True:
            screen, (left, top) = grab_screen(self.sct)
            score, center = match_image(screen, tmpl)
            found = center is not None and score >= step["threshold"]
            if t == "wait_gone":
                if not found:
                    return True
            elif found:
                if t == "click":
                    pos = (left + center[0], top + center[1])
                    rm.log("[SCENARIO] Clic sur %s en %s (%.2f)" % (os.path.basename(step["image"]), pos, score))
                    if step["button"] == "double":
                        rm.click_at(*pos)
                    rm.click_at(*pos, button="right" if step["button"] == "right" else "left")
                return True
            if time.monotonic() >= deadline:
                return False
            if self.stop_evt.wait(0.2):
                return False


# --------------------------- Fenetres annexes ---------------------------

class RegionSelector(tk.Toplevel):
    """Affiche une capture d'ecran plein ecran ; on entoure la zone a garder."""

    def __init__(self, master, img, origin, callback):
        super().__init__(master)
        self.img, self.callback = img, callback
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry("%dx%d+%d+%d" % (img.width, img.height, origin[0], origin[1]))
        self.photo = ImageTk.PhotoImage(img)
        self.canvas = tk.Canvas(self, width=img.width, height=img.height,
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.create_text(img.width // 2, 40, fill="yellow", font=("Segoe UI", 16, "bold"),
                                text="Glisse pour entourer l'image  -  Echap ou clic droit pour annuler")
        self.rect = None
        self.x0 = self.y0 = 0
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", lambda e: self._finish(None))
        self.bind("<Escape>", lambda e: self._finish(None))
        self.done = False
        self.after(50, self._take_focus)

    def _take_focus(self, tries=10):
        # Le clavier (Echap) doit arriver ici ; la fenetre n'est pas toujours prete tout de suite.
        try:
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            if tries and not self.done:
                self.after(100, lambda: self._take_focus(tries - 1))

    def _press(self, e):
        self.x0, self.y0 = e.x, e.y
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def _drag(self, e):
        self.canvas.coords(self.rect, self.x0, self.y0, e.x, e.y)

    def _release(self, e):
        x0, x1 = sorted((self.x0, e.x))
        y0, y1 = sorted((self.y0, e.y))
        if x1 - x0 < 5 or y1 - y0 < 5:
            return  # trop petit : on laisse recommencer
        try:
            path = new_image_path(time.strftime("capture_%Y%m%d_%H%M%S"))
            self.img.crop((x0, y0, x1, y1)).save(path)
        except Exception as e:
            rm.log("[!] Capture non enregistree : %s" % e)
            path = None
        self._finish(path)

    def _finish(self, path):
        if self.done:
            return
        self.done = True
        try:
            self.grab_release()
            self.destroy()
        finally:
            self.callback(path)  # toujours rappele : sinon les fenetres restent cachees


class ImagePicker(ttk.Frame):
    """Apercu d'image + boutons Capturer / Choisir un fichier. `dialog` est cache pendant la capture."""

    def __init__(self, master, gui, dialog, value=""):
        super().__init__(master)
        self.gui, self.dialog = gui, dialog
        self.var = tk.StringVar(value=value)
        self.preview = ttk.Label(self, text="(aucune image)", relief="solid", anchor="center", width=36)
        self.preview.pack(fill="x")
        row = ttk.Frame(self)
        row.pack(anchor="w", pady=(4, 0))
        ttk.Button(row, text="Capturer a l'ecran", command=self._capture).pack(side="left")
        ttk.Button(row, text="Choisir un fichier...", command=self._browse).pack(side="left", padx=6)
        ttk.Label(self, foreground="gray", wraplength=gui.px(330),
                  text="Capturer : la fenetre se cache %g s, affiche Roblox avec l'image visible, "
                       "puis entoure-la a la souris." % gui.cfg.countdown).pack(anchor="w")
        self.update_preview()

    def get(self):
        return self.var.get()

    def update_preview(self):
        path = abs_image(self.var.get()) if self.var.get() else ""
        if not path or not os.path.exists(path):
            self.preview.config(image="", text="(aucune image)")
            return
        img = Image.open(path)
        img.thumbnail((self.gui.px(320), self.gui.px(160)))
        self.photo = ImageTk.PhotoImage(img)
        self.preview.config(image=self.photo, text="")

    def _capture(self):
        self.gui.capture_region(self._captured, hide=self.dialog)

    def _captured(self, path):
        if path:
            self.var.set(rel_image(path))
            self.update_preview()

    def _browse(self):
        src = filedialog.askopenfilename(parent=self.dialog, title="Choisir une image",
                                         filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("Tous", "*.*")])
        if not src:
            return
        stem, ext = os.path.splitext(os.path.basename(src))
        dst = new_image_path(safe_name(stem) or "image", ext.lower() or ".png")
        shutil.copyfile(src, dst)
        self.var.set(rel_image(dst))
        self.update_preview()


class ReadingSetupDialog(tk.Toplevel):
    """Ce qu'il faut pour lire un ecran de fin : images des titres, icones des ressources, chiffres."""

    TITLES = (("completed", "Titre MISSION COMPLETED"), ("failed", "Titre MISSION FAILED"),
              ("obtained", "En-tete OBTAINED (au-dessus des cases)"))

    def __init__(self, gui):
        super().__init__(gui.root)
        self.gui = gui
        self.title("Configurer la lecture des fins de mission")
        self.transient(gui.root)
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        titles = ttk.Frame(f)
        titles.pack(fill="x")
        os.makedirs(os.path.join(STATS_DIR, "titles"), exist_ok=True)
        for col, (key, text) in enumerate(self.TITLES):
            box = ttk.LabelFrame(titles, text=text, padding=4)
            box.grid(row=0, column=col, sticky="nsew", padx=(0, 6))
            target = os.path.join(STATS_DIR, "titles", ms.TITLE_FILES[key])
            picker = ImagePicker(box, gui, self, rel_image(target) if os.path.exists(target) else "")
            picker.pack(fill="x")
            picker.var.trace_add("write", lambda *a, p=picker, t=target: self._title_changed(p, t))

        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, pady=8)
        icons = ttk.LabelFrame(mid, text="Icones des ressources", padding=4)
        icons.pack(side="left", fill="both", expand=True)
        self.tree = ttk.Treeview(icons, style="Thumb.Treeview", selectmode="browse", height=6, show="tree")
        self.tree.column("#0", width=gui.px(260))
        self.tree.tag_configure("unknown", foreground="#ef6c00")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.rename_icon())
        row = ttk.Frame(icons)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Renommer...", command=self.rename_icon).pack(side="left")
        ttk.Button(row, text="Supprimer", command=self.delete_icon).pack(side="left", padx=4)
        ttk.Label(icons, foreground="gray", wraplength=gui.px(300),
                  text="Les icones inconnues (en orange) apparaissent toutes seules apres une mission : "
                       "donne-leur un nom (double-clic). Deux icones du meme nom s'additionnent.").pack(anchor="w")

        digits = ttk.LabelFrame(mid, text="Chiffres et cases", padding=6)
        digits.pack(side="left", fill="both", padx=(8, 0))
        self.digits_label = ttk.Label(digits, justify="left", wraplength=gui.px(260))
        self.digits_label.pack(anchor="w")
        ttk.Button(digits, text="Calibrer les chiffres...", command=gui.open_calibration).pack(anchor="w", pady=(8, 0))
        ttk.Button(digits, text="Ouvrir le dossier stats",
                   command=lambda: os.startfile(STATS_DIR)).pack(anchor="w", pady=(4, 0))
        unread = ttk.LabelFrame(f, text="Nombres illisibles (tape la valeur affichee, puis Apprendre)", padding=6)
        unread.pack(fill="x", pady=(0, 8))
        self.unread_frame = ttk.Frame(unread)
        self.unread_frame.pack(fill="x")
        row = ttk.Frame(unread)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Apprendre", command=self.learn_unread).pack(side="left")
        ttk.Button(row, text="Tout effacer", command=self.clear_unread).pack(side="left", padx=4)
        self.unread_label = ttk.Label(unread, foreground="gray", wraplength=gui.px(640), justify="left",
                                      text="Les montants rates pendant la session en cours sont ajoutes au total "
                                           "quand tu tapes leur valeur.")
        self.unread_label.pack(anchor="w", pady=(4, 0))
        self.unread_rows = []
        ttk.Button(f, text="Fermer", command=self.destroy).pack(anchor="e")
        self.refresh()

    def _title_changed(self, picker, target):
        src = abs_image(picker.get())
        if not src or os.path.normcase(os.path.abspath(src)) == os.path.normcase(target):
            return
        try:
            shutil.copyfile(src, target)
        except OSError as e:
            rm.log("[!] Image non copiee : %s" % e)
            return
        picker.var.set(rel_image(target))
        picker.update_preview()
        self.gui.reader.reload()
        rm.log("[STATS] Image '%s' enregistree." % os.path.basename(target))
        if self.gui.watcher is not None:
            self.gui.start_mission_watch()
        self.refresh()

    UNREAD_MAX = 8
    UNREAD_FILE = re.compile(r"^(\d{8}_\d{6})_(\d+)(?:_(.*))?\.png$")

    def _unread_files(self):
        folder = os.path.join(STATS_DIR, "illisibles")
        try:
            names = [n for n in os.listdir(folder) if self.UNREAD_FILE.match(n)]
        except OSError:
            return []
        return [os.path.join(folder, n) for n in sorted(names, reverse=True)[:self.UNREAD_MAX]]

    def refresh_unread(self):
        for w in self.unread_frame.winfo_children():
            w.destroy()
        self.unread_rows = []
        files = self._unread_files()
        if not files:
            ttk.Label(self.unread_frame, text="Aucun pour l'instant.").grid(row=0, column=0, sticky="w")
            return
        for n, path in enumerate(files):
            try:
                img = Image.open(path)
                img = img.resize((img.width * 3, img.height * 3), Image.NEAREST)
            except OSError:
                continue
            m = self.UNREAD_FILE.match(os.path.basename(path))
            stamp, resource = m.group(1), m.group(3) or ""
            box = ttk.Frame(self.unread_frame, padding=3)
            box.grid(row=n // 4, column=n % 4, sticky="n")
            photo = ImageTk.PhotoImage(img)
            ttk.Label(box, image=photo).pack()
            ttk.Label(box, text="%s  %s:%s" % (resource or "?", stamp[9:11], stamp[11:13]),
                      foreground="gray").pack()
            entry = ttk.Entry(box, width=10, justify="center")
            entry.pack()
            self.unread_rows.append((path, stamp, resource, entry, photo))

    def learn_unread(self):
        gui, lines = self.gui, []
        for path, stamp, resource, entry, _ in self.unread_rows:
            text = entry.get().replace(" ", "").replace(",", "").replace(".", "")
            if not text:
                continue
            if not text.isdigit():
                lines.append("'%s' n'est pas un nombre." % entry.get())
                continue
            value = int(text)
            try:
                gui.digits.learn(ms.load_template(path), value)
            except (OSError, ValueError) as e:
                lines.append("%s : non appris (%s)." % (ms.format_amount(value), e))
                continue
            try:
                os.remove(path)
            except OSError:
                pass
            s = gui.session
            when = time.mktime(time.strptime(stamp, "%Y%m%d_%H%M%S"))
            if s is not None and resource and s.started_at is not None and when >= s.started_at - 1:
                s.totals[resource] = s.totals.get(resource, 0) + value
                gui.save_session()
                lines.append("%s %s appris et ajoute a la session." % (resource, ms.format_amount(value)))
            else:
                lines.append("%s appris." % ms.format_amount(value))
        if lines:
            rm.log("[STATS] " + " ".join(lines))
            self.unread_label.config(text="\n".join(lines))
        self.refresh()
        gui.refresh_session_views()

    def clear_unread(self):
        files = [os.path.join(STATS_DIR, "illisibles", n) for n in os.listdir(os.path.join(STATS_DIR, "illisibles"))] \
            if os.path.isdir(os.path.join(STATS_DIR, "illisibles")) else []
        if files and messagebox.askyesno("Nombres illisibles", "Effacer les %d images ?" % len(files), parent=self):
            for p in files:
                try:
                    os.remove(p)
                except OSError:
                    pass
            self.refresh_unread()

    def refresh(self):
        gui = self.gui
        self.refresh_unread()
        self.tree.delete(*self.tree.get_children())
        for key, name, path in sorted(gui.icons.items(), key=lambda i: (not i[0].startswith("inconnue"), i[1])):
            self.tree.insert("", "end", iid=key, text="  " + name, image=gui.thumb(path, (30, 30)),
                             tags=("unknown",) if name.startswith("inconnue_") else ())
        layout = gui.reader.layout
        lines = ["Taille des cases : %s" % ("%d x %d" % (layout["slot_w"], layout["slot_h"])
                                            if layout.get("slot_w") else "pas encore definie")]
        missing = gui.digits.missing()
        lines.append("Chiffres appris : tous" if not missing else
                     "Chiffres manquants : %s" % ", ".join(missing))
        self.digits_label.config(text="\n".join(lines))

    def _selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def rename_icon(self):
        key = self._selected()
        if not key:
            return
        old = self.gui.icons.names.get(key, key)
        name = simpledialog.askstring("Icone", "Nom de la ressource :", parent=self,
                                      initialvalue="" if old.startswith("inconnue_") else old)
        if not name or not name.strip() or name.strip() == old:
            return
        name = name.strip()
        self.gui.icons.rename(key, name)
        if self.gui.session is not None:
            self.gui.session.rename_resource(old, name)
            self.gui.save_session()
        rm.log("[STATS] Icone '%s' renommee en '%s'." % (old, name))
        self.refresh()
        self.gui.refresh_session_views()

    def delete_icon(self):
        key = self._selected()
        if key and messagebox.askyesno("Icone", "Supprimer l'icone '%s' ?" % self.gui.icons.names.get(key, key),
                                       parent=self):
            self.gui.icons.delete(key)
            self.refresh()


class CalibrationDialog(tk.Toplevel):
    """Apprend la taille des cases et les chiffres a partir d'une capture d'un ecran de fin."""

    ZOOM = 4

    def __init__(self, gui, screen):
        super().__init__(gui.root)
        self.gui, self.screen = gui, screen
        self.title("Calibrer les chiffres")
        self.transient(gui.root)
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        ttk.Label(f, foreground="gray", wraplength=gui.px(560),
                  text="Tape le nombre ecrit dans chaque case (sans espace ni virgule), puis Apprendre. "
                       "Laisse vide une case illisible. Refais-le sur d'autres ecrans de fin tant qu'il manque "
                       "des chiffres.").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        self.size_label = ttk.Label(row)
        self.size_label.pack(side="left")
        ttk.Button(row, text="Redefinir la taille des cases...", command=self.define_slot).pack(side="left", padx=8)
        self.slots_frame = ttk.Frame(f)
        self.slots_frame.pack(fill="x")
        self.result = ttk.Label(f, justify="left", wraplength=gui.px(560))
        self.result.pack(anchor="w", pady=6)
        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="Apprendre", command=self.learn).pack(side="left")
        ttk.Button(btns, text="Tester la lecture", command=self.test_read).pack(side="left", padx=6)
        ttk.Button(btns, text="Fermer", command=self.destroy).pack(side="right")
        self.photos, self.entries, self.numbers = [], [], []
        self.show_slots()
        if not gui.reader.layout.get("slot_w"):
            self.after(200, self.define_slot)

    def show_slots(self):
        for w in self.slots_frame.winfo_children():
            w.destroy()
        self.photos, self.entries, self.numbers = [], [], []
        layout = self.gui.reader.layout
        self.size_label.config(text="Taille des cases : %s" % (
            "%d x %d" % (layout["slot_w"], layout["slot_h"]) if layout.get("slot_w") else "pas encore definie"))
        slots = self.gui.reader.find_slots(self.screen)
        if not slots:
            ttk.Label(self.slots_frame, foreground="#c62828",
                      text="Aucune case trouvee : verifie l'en-tete OBTAINED et la taille des cases.").pack(anchor="w")
            return
        for n, rect in enumerate(slots):
            icon, number = self.gui.reader.slot_parts(self.screen, rect)
            img = Image.fromarray(np.ascontiguousarray(number[:, :, ::-1]))
            img = img.resize((img.width * self.ZOOM, img.height * self.ZOOM), Image.NEAREST)
            photo = ImageTk.PhotoImage(img)
            box = ttk.Frame(self.slots_frame, padding=4)
            box.grid(row=n // 4, column=n % 4, sticky="n")
            ttk.Label(box, image=photo).pack()
            entry = ttk.Entry(box, width=10, justify="center")
            entry.pack(pady=(4, 0))
            self.photos.append(photo)
            self.entries.append(entry)
            self.numbers.append(number)

    def define_slot(self):
        self.result.config(text="Entoure UNE case (cadre compris) sur la capture.")
        img = Image.fromarray(np.ascontiguousarray(self.screen[:, :, ::-1]))

        def done(path):
            if not path:
                return
            try:
                with Image.open(path) as cut:
                    w, h = cut.size
            finally:
                try:
                    os.remove(path)  # la capture ne sert qu'a mesurer
                except OSError:
                    pass
            layout = ms.load_layout(STATS_DIR)
            layout["slot_w"], layout["slot_h"] = w, h
            ms.save_layout(STATS_DIR, layout)
            self.gui.reader.reload()
            self.result.config(text="Taille des cases : %d x %d." % (w, h))
            self.show_slots()
            self.gui.refresh_reading_setup()

        RegionSelector(self.gui.root, img, (0, 0), done)

    def learn(self):
        lines, learned = [], []
        for n, (entry, number) in enumerate(zip(self.entries, self.numbers), 1):
            text = entry.get().replace(" ", "").replace(",", "").replace(".", "")
            if not text:
                continue
            if not text.isdigit():
                lines.append("Case %d : '%s' n'est pas un nombre, ignoree." % (n, entry.get()))
                continue
            try:
                learned += self.gui.digits.learn(number, int(text))
            except ValueError as e:
                lines.append("Case %d : chiffres mal separes (%s), ignoree." % (n, e))
        if learned:
            lines.append("Appris : %s" % ", ".join(sorted(set(learned))))
        missing = self.gui.digits.missing()
        lines.append("Tous les chiffres sont appris." if not missing else
                     "Il manque encore : %s" % ", ".join(missing))
        self.result.config(text="\n".join(lines))
        self.gui.refresh_reading_setup()

    def test_read(self):
        reader = self.gui.reader
        res = reader.read(self.screen)
        if res is None:
            values = [reader.digits.read(n) for n in self.numbers]
            head = "Pas de titre de fin reconnu (images de titre ?). Nombres lus : "
            self.result.config(text=head + ", ".join("illisible" if v is None else str(v) for v in values))
            return
        lines = ["Issue : %s" % ("reussie" if res["outcome"] == "completed" else "ratee")]
        for n, it in enumerate(res["items"], 1):
            lines.append("Case %d : %s = %s" % (n, it["name"] or "icone inconnue",
                                                 "illisible" if it["amount"] is None else it["amount"]))
        self.result.config(text="\n".join(lines))


class Hud(tk.Toplevel):
    """Petit panneau par-dessus le jeu : les clics le traversent, absent de la barre des taches."""

    def __init__(self, gui):
        super().__init__(gui.root)
        self.gui = gui
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.config(bg="#202020")
        self.label = tk.Label(self, bg="#202020", fg="#f0f0f0", font=("Segoe UI", 10), justify="left",
                              anchor="w", padx=gui.px(8), pady=gui.px(6))
        self.label.pack()
        self.text = None
        self.click_through = False
        self.apply_settings(gui.cfg.hud_corner, gui.cfg.hud_alpha)

    def _set_click_through(self):
        # WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW sur la vraie fenetre Windows (parent du widget Tk).
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(self.winfo_id())
        style = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80)
        self.click_through = True

    def apply_settings(self, corner, alpha):
        self.corner = corner if corner in HUD_CORNERS else HUD_CORNERS[0]
        # Max 0.99 : a 1.0, Tk retire WS_EX_LAYERED et le HUD ne laisserait plus passer les clics.
        self.attributes("-alpha", min(alpha, 0.99))
        if self.click_through:
            self._set_click_through()
        self._place()

    def _place(self):
        self.update_idletasks()
        m = self.gui.px(12)
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = m if self.corner.endswith("gauche") else sw - w - m
        y = m if self.corner.startswith("haut") else sh - h - m
        self.geometry("+%d+%d" % (x, y))

    def update_lines(self, lines):
        text = "\n".join(lines)
        if text != self.text:
            self.text = text
            self.label.config(text=text)
            self._place()
        if self.state() == "withdrawn":
            self.deiconify()
            self.attributes("-topmost", True)
            if not self.click_through:
                self.update_idletasks()
                self._set_click_through()

    def hide(self):
        if self.state() != "withdrawn":
            self.withdraw()


class ThresholdScale(ttk.Frame):
    """Curseur de ressemblance 0.50 - 1.00 avec sa valeur affichee."""

    def __init__(self, master, var):
        super().__init__(master)
        self.var = var
        self.label = ttk.Label(self, width=5)
        ttk.Scale(self, from_=0.5, to=1.0, variable=var, command=lambda v: self._show()).pack(
            side="left", fill="x", expand=True)
        self.label.pack(side="left", padx=4)
        self._show()

    def _show(self):
        self.label.config(text="%.2f" % self.var.get())


class TriggerDialog(tk.Toplevel):
    """Ajout / modification d'un declencheur par image."""

    def __init__(self, gui, trigger=None, is_new=None):
        """`trigger` sans `is_new` = modification ; avec is_new=True = nouveau declencheur pre-rempli."""
        super().__init__(gui.root)
        self.gui, self.result = gui, None
        self.original_name = trigger["name"] if trigger and not is_new else None
        t = normalize_trigger(trigger or {})
        self.title("Declencheur par image")
        self.transient(gui.root)
        self.resizable(False, False)

        self.actions = ([NO_MACRO] + ["Macro : " + n for n in macro_names()]
                        + ["Scenario : " + s["name"] for s in gui.scenarios])
        current = action_label(t)
        self.name = tk.StringVar(value=t["name"])
        self.action = tk.StringVar(value=current if current in self.actions else NO_MACRO)
        self.click = tk.BooleanVar(value=t["click"])
        self.threshold = tk.DoubleVar(value=t["threshold"])
        self.cooldown = tk.StringVar(value="%g" % t["cooldown"])
        self.enabled = tk.BooleanVar(value=t["enabled"])

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Nom :").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=self.name, width=36).grid(row=0, column=1, sticky="we")
        ttk.Label(f, text="Image :").grid(row=1, column=0, sticky="nw", pady=3)
        self.picker = ImagePicker(f, gui, self, t["image"])
        self.picker.grid(row=1, column=1, sticky="we", pady=3)

        ttk.Label(f, text="Action :").grid(row=2, column=0, sticky="w", pady=(10, 3))
        ttk.Combobox(f, textvariable=self.action, state="readonly", width=34,
                     values=self.actions).grid(row=2, column=1, sticky="we", pady=(10, 3))
        ttk.Checkbutton(f, text="Cliquer sur l'image avant", variable=self.click).grid(row=3, column=1, sticky="w")
        ttk.Label(f, text="Ressemblance :").grid(row=4, column=0, sticky="w", pady=3)
        ThresholdScale(f, self.threshold).grid(row=4, column=1, sticky="we")
        ttk.Label(f, text="Attente avant de\nredeclencher (s) :").grid(row=5, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=self.cooldown, width=8).grid(row=5, column=1, sticky="w")
        ttk.Checkbutton(f, text="Actif", variable=self.enabled).grid(row=6, column=1, sticky="w", pady=3)

        bottom = ttk.Frame(f)
        bottom.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(bottom, text="OK", command=self._ok).pack(side="left")
        ttk.Button(bottom, text="Annuler", command=self.destroy).pack(side="left", padx=6)
        self.grab_set()

    def _ok(self):
        name = self.name.get().strip()
        if not name:
            messagebox.showerror("Declencheur", "Donne un nom.", parent=self)
            return
        if name != self.original_name and any(t["name"] == name for t in self.gui.triggers):
            messagebox.showerror("Declencheur", "Ce nom existe deja.", parent=self)
            return
        image = self.picker.get()
        if not image or not os.path.exists(abs_image(image)):
            messagebox.showerror("Declencheur", "Choisis ou capture une image.", parent=self)
            return
        action = self.action.get()
        macro = action[len("Macro : "):] if action.startswith("Macro : ") else ""
        scenario = action[len("Scenario : "):] if action.startswith("Scenario : ") else ""
        if not macro and not scenario and not self.click.get():
            messagebox.showerror("Declencheur", "Choisis une action et/ou coche le clic.", parent=self)
            return
        try:
            cooldown = max(0.0, num(self.cooldown.get()))
        except ValueError:
            messagebox.showerror("Declencheur", "Attente invalide.", parent=self)
            return
        self.result = {"name": name, "image": image, "macro": macro, "scenario": scenario,
                       "click": self.click.get(), "threshold": round(self.threshold.get(), 2),
                       "cooldown": cooldown, "enabled": self.enabled.get()}
        self.destroy()


class StepDialog(tk.Toplevel):
    """Ajout / modification d'une etape de scenario."""

    def __init__(self, gui, step=None, step_count=1):
        super().__init__(gui.root)
        self.gui, self.result = gui, None
        s = dict(DEFAULT_STEP, **(step or {}))
        self.types = [x for x in STEP_TYPES if not (VISION_ERROR and x[0] in IMAGE_STEPS)]
        if s["type"] not in dict(self.types):
            s["type"] = self.types[0][0]
        self.title("Etape du scenario")
        self.transient(gui.root)
        self.resizable(False, False)

        self.type_var = tk.StringVar(value=dict(STEP_TYPES)[s["type"]])
        self.threshold = tk.DoubleVar(value=s["threshold"])
        self.timeout = tk.StringVar(value="%g" % s["timeout"])
        self.button = tk.StringVar(value=dict(BUTTONS)[s["button"]])
        self.macro = tk.StringVar(value=s["macro"])
        self.seconds = tk.StringVar(value="%g" % s["seconds"])
        self.after_var = tk.StringVar(value="%g" % s["after"])
        self.fail = tk.StringVar(value=dict(FAIL_ACTIONS)[s["on_fail"]])
        self.goto = tk.StringVar(value=str(s["goto"]))

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Action :").grid(row=0, column=0, sticky="w", pady=3)
        cb = ttk.Combobox(f, textvariable=self.type_var, state="readonly", width=34,
                          values=[label for _, label in self.types])
        cb.grid(row=0, column=1, sticky="we")
        cb.bind("<<ComboboxSelected>>", lambda e: self._layout())

        self.image_frame = ttk.Frame(f)
        self.image_frame.columnconfigure(1, weight=1)
        self.picker = None
        if not VISION_ERROR:
            ttk.Label(self.image_frame, text="Image :").grid(row=0, column=0, sticky="nw", pady=3)
            self.picker = ImagePicker(self.image_frame, gui, self, s["image"])
            self.picker.grid(row=0, column=1, sticky="we", pady=3)
        ttk.Label(self.image_frame, text="Ressemblance :").grid(row=1, column=0, sticky="w", pady=3)
        ThresholdScale(self.image_frame, self.threshold).grid(row=1, column=1, sticky="we")
        ttk.Label(self.image_frame, text="Attendre au plus (s) :").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(self.image_frame, textvariable=self.timeout, width=8).grid(row=2, column=1, sticky="w")
        self.button_label = ttk.Label(self.image_frame, text="Clic :")
        self.button_label.grid(row=3, column=0, sticky="w", pady=3)
        self.button_box = ttk.Combobox(self.image_frame, textvariable=self.button, state="readonly", width=16,
                                       values=[label for _, label in BUTTONS])
        self.button_box.grid(row=3, column=1, sticky="w")

        self.macro_frame = ttk.Frame(f)
        ttk.Label(self.macro_frame, text="Macro :").grid(row=0, column=0, sticky="w")
        mcb = ttk.Combobox(self.macro_frame, textvariable=self.macro, state="readonly", width=34,
                           values=macro_names())
        mcb.grid(row=0, column=1, sticky="w", padx=6)
        mcb.bind("<<ComboboxSelected>>", lambda e: self._show_duration())
        self.duration_label = ttk.Label(self.macro_frame, foreground="gray", wraplength=gui.px(360))
        self.duration_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._show_duration()

        self.wait_frame = ttk.Frame(f)
        ttk.Label(self.wait_frame, text="Duree (s) :").pack(side="left")
        ttk.Entry(self.wait_frame, textvariable=self.seconds, width=8).pack(side="left", padx=6)

        for frame in (self.image_frame, self.macro_frame, self.wait_frame):
            frame.grid(row=1, column=0, columnspan=2, sticky="we", pady=(8, 0))

        self.after_row = ttk.Frame(f)
        ttk.Label(self.after_row, text="Pause apres l'etape (s) :").pack(side="left")
        ttk.Entry(self.after_row, textvariable=self.after_var, width=8).pack(side="left", padx=6)
        self.after_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.fail_row = ttk.Frame(f)
        ttk.Label(self.fail_row, text="Si echec :").pack(side="left")
        fcb = ttk.Combobox(self.fail_row, textvariable=self.fail, state="readonly", width=28,
                           values=[label for _, label in FAIL_ACTIONS])
        fcb.pack(side="left", padx=6)
        fcb.bind("<<ComboboxSelected>>", lambda e: self._layout())
        self.goto_box = ttk.Spinbox(self.fail_row, from_=1, to=max(step_count, 1), textvariable=self.goto, width=5)
        self.goto_box.pack(side="left")
        self.fail_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=3)
        self.fail_help = ttk.Label(f, foreground="gray", wraplength=gui.px(380),
                                   text="Echec = image pas trouvee (ou toujours la) avant la limite, "
                                        "ou macro introuvable.")
        self.fail_help.grid(row=4, column=0, columnspan=2, sticky="w")

        bottom = ttk.Frame(f)
        bottom.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(bottom, text="OK", command=self._ok).pack(side="left")
        ttk.Button(bottom, text="Annuler", command=self.destroy).pack(side="left", padx=6)
        self._layout()
        self.grab_set()

    def _show_duration(self):
        name = self.macro.get()
        if not name or not os.path.exists(macro_path(name)):
            self.duration_label.config(text="")
            return
        duration = macro_info(name)[0] or 0.0
        self.duration_label.config(
            text="Duree : %.1f s (a vitesse x%g : %.1f s). Le scenario attend la fin de la macro, "
                 "puis passe a l'etape suivante (par ex. verifier qu'une image est la)."
                 % (duration, self.gui.cfg.speed, duration / self.gui.cfg.speed))

    def _type(self):
        return next(key for key, label in self.types if label == self.type_var.get())

    def _fail(self):
        return next(key for key, label in FAIL_ACTIONS if label == self.fail.get())

    def _layout(self):
        t = self._type()
        for frame, show in ((self.image_frame, t in IMAGE_STEPS), (self.macro_frame, t == "macro"),
                            (self.wait_frame, t == "wait"), (self.button_label, t == "click"),
                            (self.button_box, t == "click"), (self.after_row, t != "wait"),
                            (self.fail_row, t != "wait"), (self.fail_help, t != "wait")):
            frame.grid() if show else frame.grid_remove()
        self.goto_box.config(state="normal" if self._fail() == "goto" else "disabled")

    def _ok(self):
        t = self._type()
        try:
            timeout = max(0.0, num(self.timeout.get()))
            seconds = max(0.0, num(self.seconds.get()))
            after = max(0.0, num(self.after_var.get()))
            goto = max(1, int(self.goto.get()))
        except ValueError:
            messagebox.showerror("Etape", "Une valeur n'est pas un nombre.", parent=self)
            return
        image = self.picker.get() if self.picker else ""
        if t in IMAGE_STEPS and (not image or not os.path.exists(abs_image(image))):
            messagebox.showerror("Etape", "Choisis ou capture une image.", parent=self)
            return
        if t == "macro" and not self.macro.get():
            messagebox.showerror("Etape", "Choisis une macro.", parent=self)
            return
        self.result = {
            "type": t, "image": image if t in IMAGE_STEPS else "", "threshold": round(self.threshold.get(), 2),
            "timeout": timeout, "button": next(k for k, label in BUTTONS if label == self.button.get()),
            "macro": self.macro.get() if t == "macro" else "", "seconds": seconds, "after": after,
            "on_fail": self._fail(), "goto": goto,
        }
        self.destroy()


# ------------------------------ Moteur -------------------------------

class GuiEngine(rm.Engine):
    """Le moteur tourne dans son thread ; tout est renvoye a l'interface via sa file."""

    def __init__(self, gui):
        super().__init__(gui.cfg, gui.admin)
        self.gui = gui
        self.hotkeys = {rm.HOTKEY_RECORD, rm.HOTKEY_PLAY}  # F8 et F9 seulement ; F10 reste au jeu

    def on_hotkey(self, vk):
        self.gui.post("hotkey", vk)

    def on_recorded(self, events, duration, start_pos):
        self.gui.post("recorded", events, duration, start_pos)

    def on_state(self, state, text):
        self.gui.post("state", state, text)

    def busy(self):
        return super().busy() or self.gui.runner_active()

    def start_recording(self):
        if self.gui.runner_active():
            rm.log("Scenario en cours : arrete-le d'abord (F9).")
            return
        super().start_recording()

    def play(self, *args):
        if self.gui.runner_active():
            rm.log("Scenario en cours : arrete-le d'abord (F9).")
            return False
        return super().play(*args)


# ----------------------------- Interface -----------------------------

class Gui:
    STATE_COLORS = {"idle": "#555555", "rec": "#c62828", "play": "#2e7d32", "wait": "#ef6c00"}

    def __init__(self):
        os.makedirs(MACRO_DIR, exist_ok=True)
        self.q = queue.Queue()
        rm.set_logger(lambda msg: self.q.put(("log", str(msg))))
        settings = dict(DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {}))
        self.cfg = SimpleNamespace(debug_interval=rm.DEBUG_INTERVAL, swallow=rm.SWALLOW_HOTKEYS, **settings)
        self.admin = rm.is_admin()
        self.triggers = [normalize_trigger(t) for t in load_json(TRIGGERS_FILE, [])]
        self.scenarios = [normalize_scenario(s) for s in load_json(SCENARIOS_FILE, [])]
        self.engine = GuiEngine(self)
        self.watcher = None
        self.runner = None
        self.countdown_id = None
        self.last_macro = None
        self.fired = {}   # nom du declencheur -> (nombre de declenchements, dernier instant)
        self.thumbs = {}  # chemin d'image -> (mtime, miniature) pour la page Actif
        # Statistiques de session (missions, ressources) : voir mission_stats.py
        self.session = None
        self.mission_watcher = None
        self.last_save = 0.0
        self.uncalibrated_warned = False
        self.hud = None
        self.reading_dialog = None
        self.icons = self.digits = self.reader = None
        os.makedirs(STATS_DIR, exist_ok=True)
        recovered = pending = 0
        if not VISION_ERROR:
            recovered, pending = self._recover_sessions()
            self.icons = ms.IconLibrary(os.path.join(STATS_DIR, "icons"), os.path.join(STATS_DIR, "icons.json"))
            self.digits = ms.DigitReader(os.path.join(STATS_DIR, "digits"))
            self.reader = ms.EndScreenReader(STATS_DIR, self.icons, self.digits)

        self.root = tk.Tk()
        self.root.title("Macro Roblox" + (" (admin)" if self.admin else ""))
        self.scale = self.root.winfo_fpixels("1i") / 96.0  # 1.25 a 125 %, etc.
        self.root.geometry("%dx%d" % (self.px(820), self.px(660)))
        self.root.minsize(self.px(700), self.px(560))
        style = ttk.Style(self.root)
        style.configure("Treeview", rowheight=self.px(22))
        style.configure("Thumb.Treeview", rowheight=self.px(34))
        self.root.attributes("-topmost", self.cfg.topmost)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()

        self._import_console_macro()
        self.refresh_macros()
        self.refresh_triggers()
        self.refresh_scenarios()
        threading.Thread(target=self._engine_main, daemon=True).start()
        rm.log("Droits admin : %s" % ("oui" if self.admin else "non"))
        rm.report_roblox(self.admin)
        if VISION_ERROR:
            rm.log("Fonctions image indisponibles (%s). Installe : "
                   "pip install opencv-python numpy mss pillow" % VISION_ERROR)
        if recovered:
            rm.log("[STATS] Session precedente recuperee dans stats\\sessions.csv.")
        if pending:
            rm.log("[!] sessions.csv non modifiable (ouvert dans Excel ?) : %d session(s) precedente(s) "
                   "seront ajoutees au prochain demarrage." % pending)
        rm.log("Pret. F8 = enregistrer. F9 = arreter ce qui tourne, sinon l'action de la page ouverte.")
        self.root.after(50, self._poll)
        self.root.after(500, self._refresh_live)

    def px(self, n):
        return int(n * self.scale)

    def post(self, *msg):
        self.q.put(msg)

    def runner_active(self):
        return self.runner is not None and self.runner.is_alive()

    def _engine_main(self):
        try:
            self.engine.run()
        except Exception:
            rm.log("[!] Le moteur s'est arrete :\n" + traceback.format_exc())

    # -- construction --

    def _build(self):
        root = self.root
        menubar = tk.Menu(root)
        filemenu = tk.Menu(menubar, tearoff=False)
        filemenu.add_command(label="Exporter la configuration...", command=self.export_config)
        filemenu.add_command(label="Importer une configuration...", command=self.import_config)
        filemenu.add_separator()
        filemenu.add_command(label="Ouvrir le dossier des donnees", command=lambda: os.startfile(BASE))
        filemenu.add_separator()
        filemenu.add_command(label="Quitter", command=self.close)
        menubar.add_cascade(label="Fichier", menu=filemenu)
        root.config(menu=menubar)

        top = ttk.Frame(root, padding=(10, 8, 10, 0))
        top.pack(fill="x")
        self.state_label = tk.Label(top, text="Pret", font=("Segoe UI", 13, "bold"),
                                    fg=self.STATE_COLORS["idle"], anchor="w")
        self.state_label.pack(side="left")
        self.watch_label = tk.Label(top, text="Surveillance images : OFF", font=("Segoe UI", 10), fg="#555555")
        self.watch_label.pack(side="right")

        self.notebook = nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=10, pady=8)
        self.pages = {}  # nom de widget de l'onglet -> cle de page (pour F9)
        for key, title, build in (("actif", "Actif", self._build_actif), ("session", "Session", self._build_session),
                                  ("macros", "Macros", self._build_macros),
                                  ("scenarios", "Scenarios", self._build_scenarios),
                                  ("images", "Images", self._build_images),
                                  ("settings", "Reglages", self._build_settings)):
            frame = build(nb)
            nb.add(frame, text=title)
            self.pages[str(frame)] = key
        nb.bind("<<NotebookTabChanged>>",
                lambda e: self.refresh_session_views(history=True) if self.current_page() == "session" else None)

        logf = ttk.LabelFrame(root, text="Journal", padding=4)
        logf.pack(fill="both", padx=10, pady=(0, 10))
        self.log_text = tk.Text(logf, height=8, state="disabled", wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _build_actif(self, parent):
        f = ttk.Frame(parent, padding=8)
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.watch_button = ttk.Button(row, text="Activer la surveillance (F9)", command=self.toggle_watch)
        self.watch_button.pack(side="left")
        self.running_label = ttk.Label(row, text="En cours : rien", font=("Segoe UI", 10, "bold"))
        self.running_label.pack(side="left", padx=12)
        self.actif_tree = None
        if VISION_ERROR:
            self.watch_button.config(state="disabled")
            ttk.Label(f, foreground="#c62828", wraplength=self.px(740),
                      text="Il manque des modules pour la surveillance des images :\n\n"
                           "    pip install opencv-python numpy mss pillow").pack(anchor="w", pady=8)
            return f
        ttk.Label(f, foreground="gray", wraplength=self.px(740),
                  text="Tout ce qui peut se lancer tout seul selon ce qu'il y a a l'ecran. Surveillance activee : "
                       "quand une image active apparait et que rien ne tourne, son action se lance. "
                       "La colonne Ressemblance montre en direct ce que voit le programme (seuil a atteindre). "
                       "Double-clic = activer / desactiver.").pack(fill="x", pady=(6, 0))

        cols = ("enabled", "action", "score", "last", "count")
        self.actif_tree = ttk.Treeview(f, columns=cols, style="Thumb.Treeview", selectmode="browse", height=7)
        for col, text, width in (("#0", "Quand cette image apparait", 240), ("enabled", "Actif", 50),
                                 ("action", "Alors", 220), ("score", "Ressemblance", 120),
                                 ("last", "Dernier lancement", 120), ("count", "Fois", 45)):
            self.actif_tree.heading(col, text=text)
            self.actif_tree.column(col, width=self.px(width), anchor="w" if col in ("#0", "action") else "center")
        self.actif_tree.tag_configure("off", foreground="#999999")
        self.actif_tree.pack(fill="both", expand=True, pady=6)
        self.actif_tree.bind("<Double-1>", lambda e: self.toggle_trigger(self.actif_tree))

        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="Activer / desactiver",
                   command=lambda: self.toggle_trigger(self.actif_tree)).pack(side="left")
        ttk.Button(btns, text="Tout activer", command=lambda: self.enable_all(True)).pack(side="left", padx=4)
        ttk.Button(btns, text="Tout desactiver", command=lambda: self.enable_all(False)).pack(side="left")
        ttk.Button(btns, text="Ajouter...", command=self.add_trigger).pack(side="right")
        ttk.Button(btns, text="Modifier...",
                   command=lambda: self.edit_trigger(self.actif_tree)).pack(side="right", padx=4)
        return f

    def _build_session(self, parent):
        f = ttk.Frame(parent, padding=8)
        self.sess = None
        if VISION_ERROR:
            ttk.Label(f, foreground="#c62828", wraplength=self.px(740),
                      text="Il manque des modules pour lire les ecrans de fin de mission :\n\n"
                           "    pip install opencv-python numpy mss pillow").pack(anchor="w", pady=8)
            return f
        self.sess = sess = {}
        top = ttk.Frame(f)
        top.pack(fill="x")
        for key in ("duree", "etat", "missions"):
            sess[key] = ttk.Label(top, font=("Segoe UI", 10, "bold"))
            sess[key].pack(side="left", padx=(0, 18))
        ttk.Button(top, text="Terminer la session", command=self.end_session_button).pack(side="right")
        ttk.Button(top, text="Configurer la lecture...", command=self.open_reading_setup).pack(side="right", padx=6)
        sess["reading"] = ttk.Label(f)
        sess["reading"].pack(anchor="w", pady=(4, 0))

        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, pady=6)
        res = ttk.LabelFrame(mid, text="Ressources de la session", padding=4)
        res.pack(side="left", fill="both", expand=True)
        sess["res"] = ttk.Treeview(res, columns=("total", "rate"), height=6)
        for col, text, width in (("#0", "Ressource", 160), ("total", "Total", 110), ("rate", "Par heure", 100)):
            sess["res"].heading(col, text=text)
            sess["res"].column(col, width=self.px(width), anchor="w" if col == "#0" else "e")
        sess["res"].pack(fill="both", expand=True)
        last = ttk.LabelFrame(mid, text="Derniere mission", padding=6)
        last.pack(side="left", fill="both", padx=(8, 0))
        sess["last"] = ttk.Label(last, text="(aucune)", justify="left", width=34)
        sess["last"].pack(anchor="nw")

        hist = ttk.LabelFrame(f, text="Dernieres sessions (stats\\sessions.csv)", padding=4)
        hist.pack(fill="both", expand=True)
        cols = ("duree", "reussies", "ratees", "ressources")
        sess["hist"] = ttk.Treeview(hist, columns=cols, height=5)
        for col, text, width in (("#0", "Debut", 120), ("duree", "Duree", 70), ("reussies", "Reussies", 65),
                                 ("ratees", "Ratees", 60), ("ressources", "Ressources", 380)):
            sess["hist"].heading(col, text=text)
            sess["hist"].column(col, width=self.px(width), anchor="w" if col in ("#0", "ressources") else "center")
        sess["hist"].pack(fill="both", expand=True)
        return f

    def end_session_button(self):
        if self.session is None:
            rm.log("[STATS] Aucune session en cours.")
            return
        if messagebox.askyesno("Session", "Terminer la session et l'ajouter a l'historique ?"):
            self.end_session()

    def _refresh_session_tab(self, history):
        sess, s, now = self.sess, self.session, time.time()
        if s is None:
            sess["duree"].config(text="Duree : -")
            sess["etat"].config(text="Aucune session (active la surveillance)")
            sess["missions"].config(text="")
        else:
            total = s.completed + s.failed
            sess["duree"].config(text="Duree : %s" % ms.format_duration(s.duration(now)))
            sess["etat"].config(text="en cours" if s.running else "en pause")
            sess["missions"].config(text="Reussies %d  |  Ratees %d  |  Taux %s" % (
                s.completed, s.failed, "%d %%" % round(100 * s.completed / total) if total else "-"))
        missing = self.reader.missing()
        if missing:
            sess["reading"].config(text="Lecture : il manque %s." % ", ".join(missing), foreground="#c62828")
        elif self.mission_watcher is None and self.watcher is not None:
            sess["reading"].config(text="Lecture prete, compteur arrete.", foreground="#ef6c00")
        else:
            sess["reading"].config(text="Lecture prete.", foreground="#2e7d32")
        tree = sess["res"]
        tree.delete(*tree.get_children())
        if s is not None:
            hours = s.duration(now) / 3600
            for name, value in sorted(s.totals.items()):
                rate = ms.format_amount(value / hours) if hours >= 1 / 60 else "-"
                tree.insert("", "end", text=name, values=(ms.format_amount(value), rate))
        m = s.last_mission if s is not None else None
        if m is None:
            sess["last"].config(text="(aucune)")
        else:
            lines = ["Reussie" if m["outcome"] == "completed" else "Ratee"]
            if m.get("unstable"):
                lines[0] += " - lecture instable"
            for it in m["items"]:
                lines.append("%s : %s" % (it["name"], ms.format_amount(it["amount"])
                                          if it["amount"] is not None else "illisible"))
            sess["last"].config(text="\n".join(lines))
        if history:
            tree = sess["hist"]
            tree.delete(*tree.get_children())
            for row in ms.read_history(SESSIONS_CSV, 10):
                extra = [(k, v) for k, v in row.items() if k not in ms.CSV_FIELDS and v not in (None, "", "0")]
                tree.insert("", "end", text=row.get("debut", ""), values=(
                    row.get("duree", ""), row.get("reussies", ""), row.get("ratees", ""),
                    " · ".join("%s %s" % (k, ms.format_amount(int(v)) if v.isdigit() else v) for k, v in extra)))

    def refresh_reading_setup(self):
        d = self.reading_dialog
        if d is not None and d.winfo_exists():
            d.refresh()
        self.refresh_session_views()

    def open_calibration(self):
        """Source du calibrage : capture de l'ecran (apres le compte a rebours) ou image sur le disque."""
        parent = self.reading_dialog if self.reading_dialog is not None and self.reading_dialog.winfo_exists() \
            else self.root
        choice = messagebox.askyesnocancel(
            "Calibrer", "Oui : capturer l'ecran dans %g s (affiche un ecran de fin de mission dans Roblox).\n"
                        "Non : choisir une capture deja enregistree." % self.cfg.countdown, parent=parent)
        if choice is None:
            return
        if choice is False:
            path = filedialog.askopenfilename(parent=parent, title="Capture d'un ecran de fin",
                                              filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("Tous", "*.*")])
            if not path:
                return
            try:
                screen = load_template(path)
            except (OSError, ValueError) as e:
                messagebox.showerror("Calibrer", "Image illisible : %s" % e, parent=parent)
                return
            CalibrationDialog(self, screen)
            return
        windows = [w for w in (self.root, self.reading_dialog) if w is not None and w.winfo_exists()]
        for w in windows:
            w.withdraw()

        def grab():
            try:
                with screen_grabber() as sct:
                    screen, _ = grab_screen(sct)
            except Exception as e:
                screen = None
                rm.log("[!] Capture impossible : %s" % e)
            for w in windows:
                w.deiconify()
            if screen is not None:
                CalibrationDialog(self, screen)

        self.root.after(int(self.cfg.countdown * 1000), grab)

    def open_reading_setup(self):
        if self.reading_dialog is not None and self.reading_dialog.winfo_exists():
            self.reading_dialog.lift()
            return
        self.reading_dialog = ReadingSetupDialog(self)

    def _build_macros(self, parent):
        f = ttk.Frame(parent, padding=8)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Nom de la prochaine macro :").pack(side="left")
        self.name_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.name_var, width=28).pack(side="left", padx=6)
        self.rec_button = ttk.Button(row, text="● Enregistrer (F8)", command=self.record_button)
        self.rec_button.pack(side="left")

        cols = ("duration", "created")
        self.macro_tree = ttk.Treeview(f, columns=cols, selectmode="browse", height=10)
        self.macro_tree.heading("#0", text="Nom")
        self.macro_tree.heading("duration", text="Duree")
        self.macro_tree.heading("created", text="Creee le")
        self.macro_tree.column("#0", width=self.px(300))
        self.macro_tree.column("duration", width=self.px(80), anchor="e")
        self.macro_tree.column("created", width=self.px(150), anchor="center")
        self.macro_tree.pack(fill="both", expand=True, pady=8)
        self.macro_tree.bind("<F2>", lambda e: self.rename_macro())
        self.macro_tree.bind("<Delete>", lambda e: self.delete_macro())

        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="▶ Jouer (F9)", command=self.play_button).pack(side="left")
        ttk.Button(btns, text="■ Stop", command=self.stop_all).pack(side="left", padx=4)
        ttk.Button(btns, text="Dossier", command=lambda: os.startfile(MACRO_DIR)).pack(side="right")
        ttk.Button(btns, text="Supprimer", command=self.delete_macro).pack(side="right", padx=4)
        ttk.Button(btns, text="Renommer", command=self.rename_macro).pack(side="right")
        ttk.Label(f, foreground="gray", wraplength=self.px(740),
                  text="Avec les boutons, un compte a rebours te laisse le temps de revenir sur Roblox. "
                       "En jeu : F8 enregistre, F9 joue la macro selectionnee (page Macros ouverte) "
                       "ou arrete la lecture. Repetitions : page Reglages.").pack(
            fill="x", pady=(6, 0))
        return f

    def _build_scenarios(self, parent):
        f = ttk.Frame(parent, padding=8)
        f.columnconfigure(1, weight=1)
        f.rowconfigure(0, weight=1)

        left = ttk.Frame(f)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        ttk.Label(left, text="Scenarios", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.scen_tree = ttk.Treeview(left, show="tree", selectmode="browse", height=10)
        self.scen_tree.column("#0", width=self.px(180))
        self.scen_tree.pack(fill="y", expand=True, pady=4)
        self.scen_tree.bind("<<TreeviewSelect>>", lambda e: self.refresh_steps())
        self.scen_tree.bind("<F2>", lambda e: self.rename_scenario())
        for text, cmd in (("Nouveau", self.new_scenario), ("Renommer", self.rename_scenario),
                          ("Dupliquer", self.duplicate_scenario), ("Supprimer", self.delete_scenario)):
            ttk.Button(left, text=text, command=cmd).pack(fill="x", pady=1)

        right = ttk.Frame(f)
        right.grid(row=0, column=1, sticky="nsew")
        self.steps_title = ttk.Label(right, text="Etapes", font=("Segoe UI", 10, "bold"))
        self.steps_title.pack(anchor="w")
        cols = ("action", "detail", "fail")
        self.step_tree = ttk.Treeview(right, columns=cols, show="tree headings", style="Thumb.Treeview",
                                      selectmode="browse", height=7)
        for col, text, width in (("#0", "Etape", 105), ("action", "Action", 180), ("detail", "Detail", 230),
                                 ("fail", "Si echec", 140)):
            self.step_tree.heading(col, text=text)
            self.step_tree.column(col, width=self.px(width), anchor="w")
        self.step_tree.pack(fill="both", expand=True, pady=4)
        self.step_tree.bind("<Double-1>", lambda e: self.edit_step())
        self.step_tree.bind("<Delete>", lambda e: self.delete_step())
        self.step_tree.bind("<<TreeviewSelect>>", lambda e: self.show_step_preview())
        self.step_preview = ttk.Label(right, text="", foreground="gray", anchor="w")
        self.step_preview.pack(fill="x", pady=(0, 4))

        btns = ttk.Frame(right)
        btns.pack(fill="x")
        ttk.Button(btns, text="Ajouter...", command=self.add_step).pack(side="left")
        ttk.Button(btns, text="Modifier...", command=self.edit_step).pack(side="left", padx=4)
        ttk.Button(btns, text="↑", width=3, command=lambda: self.move_step(-1)).pack(side="left")
        ttk.Button(btns, text="↓", width=3, command=lambda: self.move_step(1)).pack(side="left", padx=4)
        ttk.Button(btns, text="Supprimer", command=self.delete_step).pack(side="right")

        run = ttk.Frame(right)
        run.pack(fill="x", pady=(10, 0))
        ttk.Label(run, text="Repetitions (0 = infini) :").pack(side="left")
        self.scen_repeat = tk.StringVar()
        entry = ttk.Entry(run, textvariable=self.scen_repeat, width=6)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda e: self.save_scenario_repeat())
        entry.bind("<FocusOut>", lambda e: self.save_scenario_repeat())
        ttk.Button(run, text="▶ Lancer (F9)", command=self.run_scenario_button).pack(side="left", padx=(12, 4))
        ttk.Button(run, text="■ Stop", command=self.stop_all).pack(side="left")
        if not VISION_ERROR:
            ttk.Button(run, text="Demarrage auto par image...", command=self.scenario_auto).pack(side="right")

        ttk.Label(f, foreground="gray", wraplength=self.px(740),
                  text="Les etapes s'executent dans l'ordre : chaque etape attend SON image (jusqu'a la limite), "
                       "meme si d'autres images sont deja visibles. \"Si echec\" choisit quoi faire si l'image "
                       "n'arrive pas : s'arreter, passer, recommencer ou sauter a une autre etape.").grid(
            row=1, column=0, columnspan=2, sticky="we", pady=(8, 0))
        return f

    def _build_images(self, parent):
        f = ttk.Frame(parent, padding=8)
        if VISION_ERROR:
            ttk.Label(f, foreground="#c62828", wraplength=self.px(740),
                      text="Il manque des modules pour cette fonction. Dans un terminal :\n\n"
                           "    pip install opencv-python numpy mss pillow\n\n"
                           "puis relance l'interface.").pack(anchor="w")
            return f
        ttk.Label(f, foreground="gray", wraplength=self.px(740),
                  text="Ici on cree et modifie les declencheurs : quand leur image apparait (ecran principal), "
                       "ils cliquent et/ou lancent une macro ou un scenario. Pour les voir en direct et activer "
                       "la surveillance : page Actif. F9 sur cette page = tester l'image selectionnee.").pack(
            fill="x")

        cols = ("enabled", "action", "click", "threshold", "cooldown")
        self.trig_tree = ttk.Treeview(f, columns=cols, style="Thumb.Treeview", selectmode="browse", height=8)
        for col, text, width in (("#0", "Nom", 190), ("enabled", "Actif", 50), ("action", "Action", 230),
                                 ("click", "Clic", 50), ("threshold", "Ressemblance", 90),
                                 ("cooldown", "Attente", 60)):
            self.trig_tree.heading(col, text=text)
            self.trig_tree.column(col, width=self.px(width), anchor="w" if col in ("#0", "action") else "center")
        self.trig_tree.pack(fill="both", expand=True, pady=8)
        self.trig_tree.bind("<Double-1>", lambda e: self.edit_trigger())
        self.trig_tree.bind("<Delete>", lambda e: self.delete_trigger())

        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="Ajouter...", command=self.add_trigger).pack(side="left")
        ttk.Button(btns, text="Modifier...", command=self.edit_trigger).pack(side="left", padx=4)
        ttk.Button(btns, text="Activer / desactiver", command=self.toggle_trigger).pack(side="left")
        ttk.Button(btns, text="Tester (F9)", command=self.test_trigger).pack(side="left", padx=4)
        ttk.Button(btns, text="Supprimer", command=self.delete_trigger).pack(side="right")
        return f

    def _build_settings(self, parent):
        f = ttk.Frame(parent, padding=12)
        self.svars = {}
        fields = (
            ("speed", "Vitesse de lecture (2 = 2x plus vite)"),
            ("sens", "Sensibilite souris a la lecture"),
            ("repeat", "Repetitions avec F9 (0 = infini)"),
            ("loop_delay", "Pause entre deux repetitions (s)"),
            ("countdown", "Compte a rebours des boutons (s)"),
            ("watch_interval", "Intervalle de recherche d'image (s)"),
        )
        for r, (key, label) in enumerate(fields):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(getattr(self.cfg, key)))
            ttk.Entry(f, textvariable=var, width=10).grid(row=r, column=1, sticky="w", padx=8)
            self.svars[key] = var
        r = len(fields)
        for key, label in (("restore_pos", "Remettre la souris a sa position de depart avant chaque lecture"),
                           ("debug", "Mode debug (affiche les deltas souris dans le journal)"),
                           ("topmost", "Fenetre toujours au premier plan"),
                           ("hud", "Afficher le HUD sur le jeu (temps, missions, ressources)")):
            var = tk.BooleanVar(value=bool(getattr(self.cfg, key)))
            ttk.Checkbutton(f, text=label, variable=var).grid(row=r, column=0, columnspan=2, sticky="w", pady=3)
            self.svars[key] = var
            r += 1
        ttk.Label(f, text="Coin du HUD").grid(row=r, column=0, sticky="w", pady=3)
        self.svars["hud_corner"] = tk.StringVar(value=self.cfg.hud_corner)
        ttk.Combobox(f, textvariable=self.svars["hud_corner"], values=HUD_CORNERS, state="readonly",
                     width=12).grid(row=r, column=1, sticky="w", padx=8)
        r += 1
        ttk.Label(f, text="Opacite du HUD (0.3 a 1)").grid(row=r, column=0, sticky="w", pady=3)
        self.svars["hud_alpha"] = tk.StringVar(value=str(self.cfg.hud_alpha))
        ttk.Entry(f, textvariable=self.svars["hud_alpha"], width=10).grid(row=r, column=1, sticky="w", padx=8)
        r += 1
        ttk.Button(f, text="Enregistrer les reglages", command=self.save_settings).grid(
            row=r, column=0, sticky="w", pady=(12, 0))
        if not self.admin:
            ttk.Button(f, text="Relancer en administrateur", command=self.relaunch_admin).grid(
                row=r + 1, column=0, sticky="w", pady=(6, 0))
            ttk.Label(f, foreground="gray", text="Seulement si Roblox tourne en administrateur.").grid(
                row=r + 2, column=0, columnspan=2, sticky="w")
        return f

    # -- boucle d'evenements venant des autres threads --

    def _poll(self):
        for _ in range(500):
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(msg)
            except Exception:
                self.append_log(traceback.format_exc())
        self.root.after(50, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "log":
            self.append_log(msg[1])
        elif kind == "state":
            self.set_state(msg[1], msg[2])
        elif kind == "hotkey":
            self.on_hotkey(msg[1])
        elif kind == "recorded":
            self.on_recorded(*msg[1:])
        elif kind == "trigger":
            self.on_trigger(*msg[1:])
        elif kind == "scenario_done":
            self.set_state("idle", "Pret")
        elif kind == "watch_stopped":
            self.watcher = None
            self.session_watch_off()
            self._show_watch()
        elif kind == "mission":
            self.on_mission(msg[1])
        elif kind == "mission_watch_stopped":
            self.mission_watcher = None
            self.refresh_session_views()

    def append_log(self, text):
        print(text)  # sans effet dans la version .exe (pas de console)
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 500:
            self.log_text.delete("1.0", "%d.0" % (lines - 500))
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def set_state(self, state, text):
        self.state_label.config(text=text, fg=self.STATE_COLORS.get(state, "#555555"))
        self.rec_button.config(text="■ Arreter (F8)" if state == "rec" else "● Enregistrer (F8)")

    # -- macros --

    def refresh_macros(self, select=None):
        select = select or self.selected_macro()
        tree = self.macro_tree
        tree.delete(*tree.get_children())
        for name in macro_names():
            duration, created = macro_info(name)
            tree.insert("", "end", iid=name, text=name,
                        values=("%.1f s" % duration if duration is not None else "?", created))
        if select and tree.exists(select):
            tree.selection_set(select)
            tree.see(select)

    def selected_macro(self):
        if not hasattr(self, "macro_tree"):
            return None
        sel = self.macro_tree.selection()
        if sel:
            return sel[0]
        if self.last_macro and os.path.exists(macro_path(self.last_macro)):
            return self.last_macro
        return None

    def load_named(self, name):
        try:
            return rm.load_macro(macro_path(name))
        except (OSError, ValueError, KeyError, IndexError) as e:
            rm.log("[!] Macro '%s' illisible : %s" % (name, e))
            return None

    def _import_console_macro(self):
        """Reprend macro.json de la version console la premiere fois."""
        old = os.path.join(BASE, rm.MACRO_FILE)
        if os.path.exists(old) and not macro_names():
            shutil.copyfile(old, macro_path("macro console"))
            rm.log("macro.json importee sous le nom 'macro console'.")

    def on_recorded(self, events, duration, start_pos):
        name = safe_name(self.name_var.get()) or time.strftime("Macro %d-%m %Hh%Mm%S")
        name = unique_name(name)
        rm.save_macro(macro_path(name), events, duration, start_pos,
                      {"created": time.strftime("%Y-%m-%d %H:%M")})
        rm.log("[REC] Sauvee dans la bibliotheque : %s" % name)
        self.name_var.set("")
        self.last_macro = name
        self.refresh_macros(select=name)

    def rename_macro(self):
        old = self.selected_macro()
        if not old:
            return
        new = simpledialog.askstring("Renommer", "Nouveau nom :", initialvalue=old, parent=self.root)
        new = safe_name(new or "")
        if not new or new == old:
            return
        if os.path.exists(macro_path(new)):
            messagebox.showerror("Renommer", "Une macro porte deja ce nom.")
            return
        os.replace(macro_path(old), macro_path(new))
        if any(t["macro"] == old for t in self.triggers):
            self.set_triggers([dict(t, macro=new) if t["macro"] == old else t for t in self.triggers])
        uses = lambda st: st["type"] == "macro" and st["macro"] == old
        if any(uses(st) for s in self.scenarios for st in s["steps"]):
            self.set_scenarios([dict(s, steps=[dict(st, macro=new) if uses(st) else st for st in s["steps"]])
                                for s in self.scenarios])
        if self.last_macro == old:
            self.last_macro = new
        self.refresh_macros(select=new)

    def delete_macro(self):
        name = self.selected_macro()
        if not name:
            return
        users = [t["name"] for t in self.triggers if t["macro"] == name]
        scen_users = [s["name"] for s in self.scenarios
                      if any(st["type"] == "macro" and st["macro"] == name for st in s["steps"])]
        text = "Supprimer la macro '%s' ?" % name
        if users:
            text += "\n\nDeclencheurs qui l'utilisent (seront desactives) : %s." % ", ".join(users)
        if scen_users:
            text += "\n\nScenarios qui l'utilisent (l'etape echouera) : %s." % ", ".join(scen_users)
        if not messagebox.askyesno("Supprimer", text):
            return
        os.remove(macro_path(name))
        if users:
            self.set_triggers([dict(t, macro="", enabled=False) if t["macro"] == name else t
                               for t in self.triggers])
        self.refresh_macros()

    # -- enregistrement / lecture --

    def start_countdown(self, label, action):
        self.cancel_countdown()
        end = time.monotonic() + self.cfg.countdown

        def tick():
            left = end - time.monotonic()
            if left <= 0:
                self.countdown_id = None
                self.set_state("idle", "Pret")
                action()
                return
            self.set_state("wait", "%s dans %.0f s... (va sur Roblox)" % (label, math.ceil(left)))
            self.countdown_id = self.root.after(100, tick)

        tick()

    def cancel_countdown(self):
        if self.countdown_id is not None:
            self.root.after_cancel(self.countdown_id)
            self.countdown_id = None
            self.set_state("idle", "Pret")

    def record_button(self):
        if self.engine.recording:
            self.engine.call(self.engine.stop_recording)
        elif self.countdown_id is None:
            self.start_countdown("Enregistrement", lambda: self.engine.call(self.engine.start_recording))

    def play_button(self):
        if self.countdown_id is None:
            self.play_selected(countdown=True)

    def play_selected(self, countdown=False):
        name = self.selected_macro()
        if not name:
            rm.log("Selectionne d'abord une macro dans la liste.")
            return
        macro = self.load_named(name)
        if macro is None:
            return
        self.last_macro = name
        start = lambda: self.engine.call(self.engine.play, *macro, self.cfg.repeat, name)
        if countdown:
            self.start_countdown("Lecture de '%s'" % name, start)
        else:
            start()

    def stop_all(self):
        self.cancel_countdown()
        if self.runner_active():
            self.runner.stop()
        self.engine.call(self.engine.stop_playback)
        self.engine.call(self.engine.stop_recording)

    def current_page(self):
        return self.pages.get(self.notebook.select())

    def on_hotkey(self, vk):
        """F9 : arrete ce qui tourne ; sinon fait l'action de la page ouverte."""
        if vk != rm.HOTKEY_PLAY:
            return
        if self.countdown_id is not None:
            self.cancel_countdown()
            return
        if self.runner_active() or self.engine.playing():
            if self.runner_active():
                self.runner.stop()
            self.engine.call(self.engine.stop_playback)
            return
        page = self.current_page()
        if page == "actif":
            self.toggle_watch()
        elif page == "macros":
            self.play_selected()
        elif page == "scenarios":
            s = self.selected_scenario()
            if s:
                self.start_scenario(s)
            else:
                rm.log("Selectionne d'abord un scenario.")
        elif page == "images" and not VISION_ERROR:
            self.test_trigger(immediate=True)
        else:
            rm.log("F9 ne fait rien sur cette page.")

    # -- scenarios --

    def set_scenarios(self, scenarios, select=None):
        self.scenarios = scenarios  # toujours une nouvelle liste : un scenario en cours garde la sienne
        save_json(SCENARIOS_FILE, scenarios)
        self.refresh_scenarios(select)

    def refresh_scenarios(self, select=None):
        tree = self.scen_tree
        select = select or (tree.selection()[0] if tree.selection() else None)
        tree.delete(*tree.get_children())
        auto = {t["scenario"] for t in self.triggers if t["enabled"] and t["scenario"]}
        for s in self.scenarios:
            tree.insert("", "end", iid=s["name"], text=s["name"] + ("   [auto]" if s["name"] in auto else ""))
        if select and tree.exists(select):
            tree.selection_set(select)
            tree.see(select)
        self.refresh_steps()

    def selected_scenario(self):
        sel = self.scen_tree.selection()
        if not sel:
            return None
        return next((s for s in self.scenarios if s["name"] == sel[0]), None)

    def replace_scenario(self, old, new, select_step=None):
        self.set_scenarios([new if x is old else x for x in self.scenarios], select=new["name"])
        if select_step is not None and self.step_tree.exists(str(select_step)):
            self.step_tree.selection_set(str(select_step))
            self.step_tree.see(str(select_step))

    def refresh_steps(self):
        s = self.selected_scenario()
        tree = self.step_tree
        sel = tree.selection()
        keep = sel[0] if sel else None
        tree.delete(*tree.get_children())
        if not s:
            self.steps_title.config(text="Etapes (choisis ou cree un scenario a gauche)")
            self.scen_repeat.set("")
            self.show_step_preview()
            return
        self.steps_title.config(text="Etapes de '%s'" % s["name"])
        self.scen_repeat.set(str(s["repeat"]))
        for i, step in enumerate(s["steps"]):
            image = self.thumb(step["image"]) if step["type"] in IMAGE_STEPS else ""
            tree.insert("", "end", iid=str(i), text=" %d" % (i + 1), image=image, values=step_text(step))
        if keep and tree.exists(keep):
            tree.selection_set(keep)
        self.show_step_preview()

    def show_step_preview(self):
        """Grand apercu de l'image de l'etape selectionnee, sous la liste."""
        s, i = self.selected_scenario(), self.selected_step()
        step = s["steps"][i] if s and i is not None and i < len(s["steps"]) else None
        photo = self.thumb(step["image"], (360, 90)) if step and step["type"] in IMAGE_STEPS else ""
        if photo:
            self.step_preview.config(image=photo, text="  Image de l'etape %d" % (i + 1), compound="left")
        elif step:
            self.step_preview.config(image="", text="Etape %d : pas d'image." % (i + 1))
        else:
            self.step_preview.config(image="", text="Selectionne une etape pour voir son image.")

    def selected_step(self):
        sel = self.step_tree.selection()
        return int(sel[0]) if sel else None

    def ask_scenario_name(self, title, initial=""):
        name = simpledialog.askstring(title, "Nom du scenario :", initialvalue=initial, parent=self.root)
        name = (name or "").strip()
        if name and name != initial and any(s["name"] == name for s in self.scenarios):
            messagebox.showerror(title, "Un scenario porte deja ce nom.")
            return None
        return name or None

    def new_scenario(self):
        name = self.ask_scenario_name("Nouveau scenario")
        if name:
            self.set_scenarios(self.scenarios + [{"name": name, "repeat": 1, "steps": []}], select=name)

    def rename_scenario(self):
        s = self.selected_scenario()
        if not s:
            return
        name = self.ask_scenario_name("Renommer", s["name"])
        if not name or name == s["name"]:
            return
        if any(t["scenario"] == s["name"] for t in self.triggers):
            self.set_triggers([dict(t, scenario=name) if t["scenario"] == s["name"] else t for t in self.triggers])
        self.replace_scenario(s, dict(s, name=name))

    def duplicate_scenario(self):
        s = self.selected_scenario()
        if not s:
            return
        names = {x["name"] for x in self.scenarios}
        name, n = "%s (copie)" % s["name"], 2
        while name in names:
            name = "%s (copie %d)" % (s["name"], n)
            n += 1
        self.set_scenarios(self.scenarios + [dict(copy.deepcopy(s), name=name)], select=name)

    def delete_scenario(self):
        s = self.selected_scenario()
        if not s:
            return
        users = [t["name"] for t in self.triggers if t["scenario"] == s["name"]]
        text = "Supprimer le scenario '%s' ?" % s["name"]
        if users:
            text += "\n\nDeclencheurs qui l'utilisent (seront desactives) : %s." % ", ".join(users)
        if not messagebox.askyesno("Supprimer", text):
            return
        if users:
            self.set_triggers([dict(t, scenario="", enabled=False) if t["scenario"] == s["name"] else t
                               for t in self.triggers])
        self.set_scenarios([x for x in self.scenarios if x is not s])

    def save_scenario_repeat(self):
        s = self.selected_scenario()
        if not s:
            return
        try:
            repeat = max(0, int(self.scen_repeat.get()))
        except ValueError:
            self.scen_repeat.set(str(s["repeat"]))
            return
        if repeat != s["repeat"]:
            self.replace_scenario(s, dict(s, repeat=repeat), self.selected_step())

    def add_step(self):
        s = self.selected_scenario()
        if not s:
            messagebox.showinfo("Etape", "Cree ou choisis d'abord un scenario a gauche.")
            return
        dlg = StepDialog(self, None, len(s["steps"]) + 1)
        self.root.wait_window(dlg)
        if dlg.result:
            i = self.selected_step()
            i = len(s["steps"]) if i is None else i + 1  # insere apres l'etape selectionnee
            steps = s["steps"][:i] + [dlg.result] + s["steps"][i:]
            self.replace_scenario(s, dict(s, steps=steps), i)

    def edit_step(self):
        s, i = self.selected_scenario(), self.selected_step()
        if s is None or i is None:
            return
        dlg = StepDialog(self, s["steps"][i], len(s["steps"]))
        self.root.wait_window(dlg)
        if dlg.result:
            steps = list(s["steps"])
            steps[i] = dlg.result
            self.replace_scenario(s, dict(s, steps=steps), i)

    def move_step(self, delta):
        s, i = self.selected_scenario(), self.selected_step()
        if s is None or i is None or not 0 <= i + delta < len(s["steps"]):
            return
        steps = list(s["steps"])
        steps[i], steps[i + delta] = steps[i + delta], steps[i]
        self.replace_scenario(s, dict(s, steps=steps), i + delta)

    def delete_step(self):
        s, i = self.selected_scenario(), self.selected_step()
        if s is None or i is None:
            return
        if messagebox.askyesno("Supprimer", "Supprimer l'etape %d ?" % (i + 1)):
            steps = s["steps"][:i] + s["steps"][i + 1:]
            self.replace_scenario(s, dict(s, steps=steps), min(i, len(steps) - 1) if steps else None)

    def run_scenario_button(self):
        s = self.selected_scenario()
        if not s:
            rm.log("Selectionne d'abord un scenario.")
        elif self.countdown_id is None:
            self.start_countdown("Scenario '%s'" % s["name"], lambda: self.start_scenario(s))

    def scenario_auto(self):
        """Declencheur par image qui lance ce scenario (le cree, ou modifie celui qui existe)."""
        s = self.selected_scenario()
        if not s:
            messagebox.showinfo("Demarrage auto", "Choisis d'abord un scenario a gauche.")
            return
        existing = next((t for t in self.triggers if t["scenario"] == s["name"]), None)
        if existing:
            dlg = TriggerDialog(self, existing)
        else:
            name, n = "Auto : %s" % s["name"], 2
            while any(t["name"] == name for t in self.triggers):
                name = "Auto : %s (%d)" % (s["name"], n)
                n += 1
            dlg = TriggerDialog(self, {"name": name, "scenario": s["name"], "click": False}, is_new=True)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        if existing:
            self.set_triggers([dlg.result if x is existing else x for x in self.triggers])
        else:
            self.set_triggers(self.triggers + [dlg.result])
        rm.log("Demarrage auto enregistre : '%s' se lancera quand son image apparait "
               "(surveillance a activer dans la page Actif)." % s["name"])

    def start_scenario(self, s, pre_click=None):
        if self.engine.busy():
            rm.log("Deja occupe (lecture, enregistrement ou scenario) : arrete d'abord.")
            return
        if not s["steps"]:
            rm.log("Le scenario '%s' n'a aucune etape." % s["name"])
            return
        self.runner = ScenarioRunner(self, s, pre_click)
        self.runner.start()

    # -- declencheurs par image --

    def set_triggers(self, triggers):
        self.triggers = triggers  # nouvelle liste : le thread de surveillance lit l'ancienne sans risque
        save_json(TRIGGERS_FILE, triggers)
        self.refresh_triggers()
        self.refresh_scenarios()  # marque [auto] des scenarios

    def refresh_triggers(self, select=None):
        if VISION_ERROR:
            return
        self.refresh_actif()
        tree = self.trig_tree
        select = select or (tree.selection()[0] if tree.selection() else None)
        tree.delete(*tree.get_children())
        for t in self.triggers:
            tree.insert("", "end", iid=t["name"], text=" " + t["name"], image=self.thumb(t["image"]), values=(
                "oui" if t["enabled"] else "non", action_label(t), "oui" if t["click"] else "non",
                "%.2f" % t["threshold"], "%g s" % t["cooldown"]))
        if select and tree.exists(select):
            tree.selection_set(select)

    def selected_trigger(self, tree=None):
        sel = (tree or self.trig_tree).selection()
        if not sel:
            return None
        return next((t for t in self.triggers if t["name"] == sel[0]), None)

    def thumb(self, image, size=(64, 30)):
        """Miniature (gardee en cache) d'une image ; "" si pas d'image."""
        if VISION_ERROR or not image:
            return ""
        path = abs_image(image)
        key = (path, size)
        try:
            mtime = os.path.getmtime(path)
            cached = self.thumbs.get(key)
            if cached and cached[0] == mtime:
                return cached[1]
            img = Image.open(path)
            img.thumbnail((self.px(size[0]), self.px(size[1])))
            photo = ImageTk.PhotoImage(img)
        except (OSError, ValueError):
            return ""
        self.thumbs[key] = (mtime, photo)
        return photo

    def refresh_actif(self):
        tree = self.actif_tree
        if tree is None:
            return
        sel = tree.selection()
        keep = sel[0] if sel else None
        tree.delete(*tree.get_children())
        for t in sorted(self.triggers, key=lambda t: not t["enabled"]):  # actifs en haut
            parts = ["Clic"] if t["click"] else []
            if t["scenario"] or t["macro"]:
                parts.append(action_label(t))
            tree.insert("", "end", iid=t["name"], text=" " + t["name"], image=self.thumb(t["image"]),
                        values=("oui" if t["enabled"] else "non", " puis ".join(parts), "", "", ""),
                        tags=() if t["enabled"] else ("off",))
        if keep and tree.exists(keep):
            tree.selection_set(keep)
        self._update_live()

    def _update_live(self):
        """Etat en direct de la page Actif : ce qui tourne + ressemblance mesuree pour chaque image."""
        running = self.current_activity()[1]
        self.running_label.config(text="En cours : " + running)
        tree, w, now = self.actif_tree, self.watcher, time.time()
        if tree is None:
            return
        for t in self.triggers:
            name = t["name"]
            if not tree.exists(name):
                continue
            if not t["enabled"]:
                score = "-"
            elif w is None:
                score = "surveillance off"
            elif w.paused:
                score = "en pause"
            elif name not in w.scores:
                score = "..."
            else:
                s = w.scores[name]
                score = "%.2f / %.2f%s" % (s, t["threshold"], "  OK" if s >= t["threshold"] else "")
            count, last = self.fired.get(name, (0, None))
            if last is None:
                ago = "jamais"
            elif now - last < 60:
                ago = "il y a %d s" % (now - last)
            elif now - last < 3600:
                ago = "il y a %d min" % ((now - last) // 60)
            else:
                ago = "il y a %d h" % ((now - last) // 3600)
            tree.set(name, "score", score)
            tree.set(name, "last", ago)
            tree.set(name, "count", count)

    def current_activity(self):
        """(principal, detail) : principal = nom du scenario / de la macro ("" si rien), detail = texte affiche."""
        if self.engine.recording:
            return "enregistrement", "enregistrement (F8 pour arreter)"
        if self.runner_active():
            return self.runner.scenario["name"], self.runner.current
        if self.engine.playing():
            name = self.engine.player.name or "macro"
            return name, "macro %s" % (self.engine.player.name or "")
        if self.countdown_id is not None:
            return "", "compte a rebours..."
        return "", "rien"

    def _refresh_live(self):
        try:
            self._update_live()
            if self.session is not None:
                now = time.time()
                self.session.set_activity(*self.current_activity())
                if now - self.last_save >= 60:
                    self.save_session(now)
            self.refresh_session_views()
        except tk.TclError:
            return  # fenetre fermee
        self.root.after(500, self._refresh_live)

    # -- statistiques de session --

    @staticmethod
    def _recover_sessions():
        """Sessions restees en cours (plantage) -> sessions.csv. Renvoie (recuperees, en attente).
        Si le CSV est verrouille, la sauvegarde est mise de cote pour ne pas etre ecrasee par la nouvelle session."""
        recovered = pending = 0
        base = os.path.splitext(os.path.basename(CURRENT_SESSION))[0]
        for name in sorted(os.listdir(STATS_DIR)):
            if not (name.startswith(base) and name.endswith(".json")):
                continue
            path = os.path.join(STATS_DIR, name)
            try:
                if ms.recover_current(path, SESSIONS_CSV):
                    recovered += 1
                elif os.path.exists(path):
                    pending += 1
                    if path == CURRENT_SESSION:
                        os.replace(path, os.path.join(STATS_DIR, "%s_%s.json" % (base, time.strftime("%Y%m%d_%H%M%S"))))
            except OSError:
                pending += 1
        return recovered, pending

    def save_session(self, now=None):
        if self.session is None:
            return
        now = time.time() if now is None else now
        try:
            ms.save_current(CURRENT_SESSION, self.session, now)
        except OSError as e:
            rm.log("[!] Session non sauvegardee : %s" % e)
        self.last_save = now

    def start_mission_watch(self):
        if self.reader is None or self.mission_watcher is not None:
            return
        self.reader.reload()
        missing = [m for m in self.reader.missing() if m in ("titre reussi", "titre rate")]
        if missing:
            rm.log("[STATS] Compteur de missions inactif : il manque %s (onglet Session)." % ", ".join(missing))
            return
        self.mission_watcher = MissionWatcher(self)
        self.mission_watcher.start()

    def stop_mission_watch(self):
        if self.mission_watcher is not None:
            self.mission_watcher.stop()
            self.mission_watcher = None

    def session_watch_on(self):
        """Surveillance activee : la session demarre, ou reprend apres une pause."""
        if VISION_ERROR:
            return
        now = time.time()
        if self.session is None:
            self.session = ms.Session()
            self.session.start(now)
            self.uncalibrated_warned = False
            rm.log("[STATS] Session demarree.")
        else:
            self.session.resume(now)
        self.start_mission_watch()
        self.save_session(now)

    def session_watch_off(self):
        """Surveillance coupee : le chrono se met en pause, la session continue."""
        self.stop_mission_watch()
        if self.session is not None:
            now = time.time()
            self.session.pause(now)
            self.save_session(now)

    def end_session(self):
        s = self.session
        if s is None:
            return
        now = time.time()
        s.end(now)
        if s.should_write(now):
            try:
                ms.append_session_csv(SESSIONS_CSV, s.csv_row(now))
            except OSError as e:
                rm.log("[!] sessions.csv non ecrit (ouvert dans Excel ?) : %s" % e)
                self.save_session(now)
                return
        try:
            os.remove(CURRENT_SESSION)
        except OSError:
            pass
        rm.log("[STATS] Session terminee : %s, %d reussies, %d ratees."
               % (ms.format_duration(s.duration(now)), s.completed, s.failed))
        self.session = None
        if self.watcher is not None:
            self.session_watch_on()  # la surveillance continue : nouvelle session
        self.refresh_session_views(history=True)

    def on_mission(self, result):
        s = self.session
        if s is None:
            return
        uncalibrated = any(m in self.reader.missing() for m in ("chiffres", "taille de case"))
        stamp = time.strftime("%Y%m%d_%H%M%S")
        for i, item in enumerate(result["items"]):
            if item["key"] is None:
                key, name, _ = self.icons.identify(item["icon"])
                if key is None:
                    key = self.icons.add_unknown(item["icon"])
                    name = key
                    rm.log("[STATS] Nouvelle icone '%s' : donne-lui un nom dans l'onglet Session." % key)
                item["key"], item["name"] = key, name
            if item["amount"] is None and not uncalibrated:
                folder = os.path.join(STATS_DIR, "illisibles")
                os.makedirs(folder, exist_ok=True)
                try:
                    ms.save_image(os.path.join(folder, "%s_%d_%s.png" % (stamp, i + 1, safe_name(item["name"] or ""))),
                                  item["number"])
                except (OSError, ValueError):
                    pass
        if uncalibrated and not self.uncalibrated_warned:
            self.uncalibrated_warned = True
            rm.log("[STATS] Chiffres non calibres : ressources ignorees (onglet Session > Calibrer).")
        s.record(result)
        self.save_session()
        parts = ["%s %s" % (it["name"], ms.format_amount(it["amount"]) if it["amount"] is not None else "illisible")
                 for it in result["items"]]
        rm.log("[STATS] Mission %s%s%s" % ("reussie" if result["outcome"] == "completed" else "ratee",
                                          " : " + ", ".join(parts) if parts else "",
                                          " (lecture instable)" if result.get("unstable") else ""))
        self.refresh_session_views()

    def refresh_session_views(self, history=False):
        """HUD + onglet Session."""
        if VISION_ERROR:
            return
        if self.hud is None:
            self.hud = Hud(self)
        if self.cfg.hud and self.session is not None:
            self.hud.update_lines(ms.hud_lines(self.session, time.time()))
        else:
            self.hud.hide()
        if self.sess is not None and self.current_page() == "session":
            self._refresh_session_tab(history)

    def enable_all(self, enabled):
        self.set_triggers([dict(t, enabled=enabled) for t in self.triggers])

    def add_trigger(self):
        dlg = TriggerDialog(self)
        self.root.wait_window(dlg)
        if dlg.result:
            self.set_triggers(self.triggers + [dlg.result])
            self.refresh_triggers(select=dlg.result["name"])

    def edit_trigger(self, tree=None):
        t = self.selected_trigger(tree)
        if not t:
            return
        dlg = TriggerDialog(self, t)
        self.root.wait_window(dlg)
        if dlg.result:
            self.set_triggers([dlg.result if x is t else x for x in self.triggers])
            self.refresh_triggers(select=dlg.result["name"])

    def toggle_trigger(self, tree=None):
        t = self.selected_trigger(tree)
        if t:
            self.set_triggers([dict(x, enabled=not x["enabled"]) if x is t else x for x in self.triggers])

    def delete_trigger(self):
        t = self.selected_trigger()
        if t and messagebox.askyesno("Supprimer", "Supprimer le declencheur '%s' ?" % t["name"]):
            self.set_triggers([x for x in self.triggers if x is not t])

    def capture_region(self, callback, hide=None):
        """Cache la fenetre principale (et `hide`, la boite de dialogue), attend le compte a rebours,
        fait entourer une zone de l'ecran, puis reaffiche tout et appelle callback(chemin ou None)."""
        windows = [self.root] + ([hide] if hide is not None else [])
        if hide is not None:
            hide.grab_release()
        for w in windows:
            w.withdraw()
        done = []

        def finish(path):
            if done:
                return
            done.append(True)
            self.show_again(windows)
            try:
                callback(path)
            except Exception:
                rm.log(traceback.format_exc())

        def grab():
            try:
                with screen_grabber() as sct:
                    mon = primary_monitor(sct)
                    shot = sct.grab(mon)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                RegionSelector(self.root, img, (mon["left"], mon["top"]), finish)
            except Exception as e:
                rm.log("[!] Capture impossible : %s" % e)
                finish(None)

        self.root.after(int(self.cfg.countdown * 1000), grab)

    def show_again(self, windows):
        """Reaffiche les fenetres cachees ; la derniere (boite de dialogue) reprend la main.
        Le grab peut echouer tant que Windows n'a pas fini de l'afficher : on reessaie."""
        for w in windows:
            try:
                w.deiconify()
                w.lift()
            except tk.TclError:
                pass
        last = windows[-1]

        def regrab(tries=20):
            try:
                if last is not self.root:
                    last.grab_set()
                last.focus_force()
            except tk.TclError:
                if tries:
                    self.root.after(100, lambda: regrab(tries - 1))

        self.root.after(50, regrab)

    def test_trigger(self, immediate=False):
        """Bouton : cache la fenetre et attend le compte a rebours. F9 en jeu : teste tout de suite."""
        t = self.selected_trigger()
        if not t:
            rm.log("Selectionne d'abord un declencheur dans la page Images.")
            return
        if not immediate:
            self.root.withdraw()

        def run_test():
            try:
                tmpl = load_template(abs_image(t["image"]))
                with screen_grabber() as sct:
                    screen, (left, top) = grab_screen(sct)
                score, center = match_image(screen, tmpl)
            except Exception as e:
                self.root.deiconify()
                messagebox.showerror("Test", "Erreur : %s" % e)
                return
            self.root.deiconify()
            if center and score >= t["threshold"]:
                text = "Trouvee en (%d, %d)\nRessemblance %.2f (seuil %.2f)" % (
                    left + center[0], top + center[1], score, t["threshold"])
            else:
                text = ("Pas trouvee.\nMeilleure ressemblance %.2f (seuil %.2f).\n\n"
                        "Si l'image est bien a l'ecran, baisse un peu le seuil." % (score, t["threshold"]))
            rm.log("[TEST] %s : %s" % (t["name"], text.replace("\n", " ")))
            messagebox.showinfo("Test : %s" % t["name"], text)

        if immediate:
            run_test()
        else:
            rm.log("Test dans %g s : affiche Roblox..." % self.cfg.countdown)
            self.root.after(int(self.cfg.countdown * 1000), run_test)

    def toggle_watch(self):
        if VISION_ERROR:
            rm.log("Surveillance impossible : pip install opencv-python numpy mss pillow")
            return
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
            rm.log("[IMAGE] Surveillance desactivee.")
            self.session_watch_off()
        else:
            if not any(t["enabled"] for t in self.triggers):
                rm.log("[IMAGE] Aucun declencheur actif : ajoute-en un (page Images ou Actif).")
            self.watcher = Watcher(self)
            self.watcher.start()
            rm.log("[IMAGE] Surveillance activee.")
            self.session_watch_on()
        self._show_watch()
        self._update_live()

    def _show_watch(self):
        on = self.watcher is not None
        self.watch_label.config(text="Surveillance images : %s" % ("ON" if on else "OFF"),
                                fg="#2e7d32" if on else "#555555")
        if not VISION_ERROR:
            self.watch_button.config(text="Desactiver la surveillance (F9)" if on else "Activer la surveillance (F9)")

    def on_trigger(self, t, pos, score):
        if self.watcher is None or self.engine.busy():
            return  # surveillance coupee ou autre chose a demarre entre-temps
        rm.log("[IMAGE] '%s' trouvee (%.2f) en %s" % (t["name"], score, pos))
        self.fired[t["name"]] = (self.fired.get(t["name"], (0, None))[0] + 1, time.time())
        if t["scenario"]:
            s = next((x for x in self.scenarios if x["name"] == t["scenario"]), None)
            if s is None:
                rm.log("[!] Scenario '%s' introuvable." % t["scenario"])
            else:
                self.start_scenario(s, pre_click=pos if t["click"] else None)
            return
        macro = None
        if t["macro"]:
            macro = self.load_named(t["macro"])
            if macro is None:
                return
        engine = self.engine

        def act():  # execute dans le thread du moteur
            if engine.busy():
                return
            if t["click"]:
                rm.click_at(*pos)
                time.sleep(0.05)
            if macro:
                engine.play(*macro, 1, t["macro"])

        engine.call(act)

    # -- export / import --

    def current_settings(self):
        return {key: getattr(self.cfg, key) for key in DEFAULT_SETTINGS}

    def export_config(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Exporter la configuration", defaultextension=".zip",
            initialfile=time.strftime("config_macro_roblox_%Y-%m-%d.zip"), filetypes=[("Configuration", "*.zip")])
        if not path:
            return
        try:
            n_macros, n_images = export_config(path, self.triggers, self.scenarios, self.current_settings())
        except OSError as e:
            messagebox.showerror("Exporter", "Export impossible : %s" % e)
            return
        rm.log("Configuration exportee : %d macros, %d scenarios, %d declencheurs, %d images -> %s"
               % (n_macros, len(self.scenarios), len(self.triggers), n_images, path))
        messagebox.showinfo("Exporter", "Configuration exportee dans :\n%s" % path)

    def import_config(self):
        path = filedialog.askopenfilename(parent=self.root, title="Importer une configuration",
                                          filetypes=[("Configuration", "*.zip")])
        if not path:
            return
        settings = None
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if "manifest.json" not in names:
                    raise ValueError("ce fichier n'est pas une configuration exportee par l'application")
                read = lambda n: json.loads(z.read(n).decode("utf-8")) if n in names else None
                triggers = [normalize_trigger(t) for t in read("triggers.json") or []]
                scenarios = [normalize_scenario(s) for s in read("scenarios.json") or []]
                # Uniquement des fichiers a plat dans macros/ et images/ (pas de chemins bizarres).
                macros = sorted(n for n in names if n.startswith("macros/") and n.count("/") == 1
                                and n.lower().endswith(".json"))
                images = sorted(n for n in names if n.startswith("images/") and n.count("/") == 1 and len(n) > 7)
                conflicts = sum(os.path.exists(macro_path(safe_name(n[7:-5]))) for n in macros)
                conflicts += sum(any(x["name"] == t["name"] for x in self.triggers) for t in triggers)
                conflicts += sum(any(x["name"] == s["name"] for x in self.scenarios) for s in scenarios)
                text = "Importer %d macros, %d scenarios, %d declencheurs et %d images ?" % (
                    len(macros), len(scenarios), len(triggers), len(images))
                if conflicts:
                    text += "\n\n%d element(s) du meme nom seront remplaces." % conflicts
                if not messagebox.askyesno("Importer", text):
                    return
                if "settings.json" in names and messagebox.askyesno("Importer", "Importer aussi les reglages ?"):
                    settings = read("settings.json")

                mapping = {}
                for n in images:
                    data = z.read(n)
                    stem, ext = os.path.splitext(safe_name(n[7:]) or "image.png")
                    dst = os.path.join(IMAGE_DIR, stem + ext)
                    if os.path.exists(dst):
                        with open(dst, "rb") as f:
                            if f.read() != data:
                                dst = new_image_path(stem, ext)  # autre image du meme nom : on renomme
                    if not os.path.exists(dst):
                        os.makedirs(IMAGE_DIR, exist_ok=True)
                        with open(dst, "wb") as f:
                            f.write(data)
                    mapping[n] = rel_image(dst)
                for obj in image_objects(triggers, scenarios):
                    obj["image"] = mapping.get(obj["image"].replace("\\", "/"), obj["image"])
                for n in macros:
                    with open(macro_path(safe_name(n[7:-5]) or "Macro"), "wb") as f:
                        f.write(z.read(n))
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as e:
            messagebox.showerror("Importer", "Import impossible : %s" % e)
            return
        self.set_triggers(merge_by_name(self.triggers, triggers))
        self.set_scenarios(merge_by_name(self.scenarios, scenarios))
        if settings:
            self.apply_settings(dict(self.current_settings(),
                                     **{k: v for k, v in settings.items() if k in DEFAULT_SETTINGS}))
        self.refresh_macros()
        rm.log("Configuration importee : %d macros, %d scenarios, %d declencheurs, %d images%s."
               % (len(macros), len(scenarios), len(triggers), len(images), ", reglages" if settings else ""))

    # -- reglages --

    def apply_settings(self, values):
        for key, value in values.items():
            setattr(self.cfg, key, value)
            self.svars[key].set(value)
        self.root.attributes("-topmost", self.cfg.topmost)
        if self.hud is not None:
            self.hud.apply_settings(self.cfg.hud_corner, self.cfg.hud_alpha)
        self.refresh_session_views()
        save_json(SETTINGS_FILE, values)

    def save_settings(self):
        new = {}
        try:
            for key in ("speed", "sens", "loop_delay", "countdown", "watch_interval", "hud_alpha"):
                new[key] = num(self.svars[key].get())
            new["repeat"] = int(self.svars["repeat"].get())
        except ValueError:
            messagebox.showerror("Reglages", "Une valeur n'est pas un nombre.")
            return
        if new["speed"] <= 0 or new["repeat"] < 0 or new["watch_interval"] <= 0 or new["countdown"] < 0:
            messagebox.showerror("Reglages", "Vitesse et intervalle > 0, repetitions et compte a rebours >= 0.")
            return
        if not 0.3 <= new["hud_alpha"] <= 1.0:
            messagebox.showerror("Reglages", "L'opacite du HUD doit etre entre 0.3 et 1.")
            return
        for key in ("restore_pos", "debug", "topmost", "hud"):
            new[key] = bool(self.svars[key].get())
        new["hud_corner"] = self.svars["hud_corner"].get()
        self.apply_settings(new)
        rm.log("Reglages enregistres.")

    def relaunch_admin(self):
        if rm.relaunch_as_admin(os.path.abspath(__file__), sys.argv[1:]):
            self.close()

    def close(self):
        self.stop_mission_watch()
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self.end_session()
        if self.runner_active():
            self.runner.stop()
        self.engine.quit()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    if sys.platform != "win32":
        sys.exit("Windows uniquement.")
    if "--admin" in sys.argv and not rm.is_admin():
        rm.relaunch_as_admin(os.path.abspath(__file__), [a for a in sys.argv[1:] if a != "--admin"])
        return
    try:
        Gui().run()
    except Exception:
        # Dans la version .exe il n'y a pas de console : on garde une trace lisible.
        err = traceback.format_exc()
        try:
            with open(os.path.join(BASE, "crash.log"), "w", encoding="utf-8") as f:
                f.write(err)
            messagebox.showerror("Macro Roblox", "Erreur (details dans crash.log) :\n\n" + err[-1500:])
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
