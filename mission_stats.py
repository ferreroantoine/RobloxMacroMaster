# -*- coding: utf-8 -*-
"""
mission_stats.py - statistiques de session pour macro_gui.py.

Lit l'ecran de fin de mission (MISSION COMPLETED / FAILED + cases OBTAINED), compte chaque
mission une seule fois et additionne les ressources obtenues. Aucune interface ici : macro_gui.py
s'occupe de l'affichage (HUD, onglet Session).

Dependances : opencv-python, numpy (deja requises par macro_gui.py).
"""

import csv
import json
import math
import os
import re
import threading
import time

import cv2
import numpy as np


# ------------------------------ Images --------------------------------

def load_template(path):
    data = np.fromfile(path, dtype=np.uint8)  # cv2.imread ne gere pas les accents dans les chemins
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("image illisible")
    return img


def save_image(path, img):
    ok, data = cv2.imencode(".png", img)
    if not ok:
        raise ValueError("image non encodable")
    data.tofile(path)


def match_image(screen, tmpl):
    """(score 0..1, centre (x, y) dans la capture) de la meilleure correspondance."""
    th, tw = tmpl.shape[:2]
    if th > screen.shape[0] or tw > screen.shape[1]:
        return 0.0, None
    if tmpl.reshape(-1, 3).std(axis=0).max() < 2:
        # Image (presque) unie : CCOEFF n'a pas de sens, on compare les couleurs directement.
        res = cv2.matchTemplate(screen, tmpl, cv2.TM_SQDIFF_NORMED)
        best, _, loc, _ = cv2.minMaxLoc(res)
        score = 1.0 - best
    else:
        res = cv2.matchTemplate(screen, tmpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
    if not math.isfinite(score):
        score = 0.0
    return score, (loc[0] + tw // 2, loc[1] + th // 2)


def _same_size_score(a, b):
    """Ressemblance (TM_CCOEFF_NORMED) de deux images de meme taille."""
    if a.std() == 0 or b.std() == 0:
        return 1.0 if a.std() == b.std() and abs(float(a.mean()) - float(b.mean())) < 1 else 0.0
    score = float(cv2.matchTemplate(a.astype(np.float32), b.astype(np.float32), cv2.TM_CCOEFF_NORMED)[0, 0])
    return score if math.isfinite(score) else 0.0


# ------------------------------ Chiffres ------------------------------

DIGITS = "0123456789"
_DIGIT_FILE = re.compile(r"^(\d)(?:_(\d+))?\.png$")


class DigitReader:
    """Lit un entier ecrit en clair (blanc / gris) avec des modeles de chiffres appris dans le jeu."""

    def __init__(self, digits_dir, threshold=0.70, min_value=170, max_saturation=80):
        self.dir = digits_dir
        self.threshold, self.min_value, self.max_saturation = threshold, min_value, max_saturation
        self.templates = []  # [(chiffre, masque)]
        self.reload()

    def reload(self):
        self.templates = []
        if not os.path.isdir(self.dir):
            return
        for name in sorted(os.listdir(self.dir)):
            m = _DIGIT_FILE.match(name)
            if m:
                try:
                    img = load_template(os.path.join(self.dir, name))
                except (OSError, ValueError):
                    continue
                self.templates.append((m.group(1), cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)))

    def missing(self):
        have = {d for d, _ in self.templates}
        return [d for d in DIGITS if d not in have]

    def segment(self, roi_bgr):
        """Masques des caracteres, de gauche a droite ; virgules et points (petites taches) retires."""
        hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        mask = ((hsv[:, :, 2] >= self.min_value) & (hsv[:, :, 1] <= self.max_saturation)).astype(np.uint8) * 255
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        comps = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= 3]
        if not comps:
            return []
        max_h = max(stats[i, cv2.CC_STAT_HEIGHT] for i in comps)
        comps = [i for i in comps if stats[i, cv2.CC_STAT_HEIGHT] >= 0.5 * max_h]
        comps.sort(key=lambda i: stats[i, cv2.CC_STAT_LEFT])
        glyphs = []
        for i in comps:
            x, y, w, h = (stats[i, k] for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP,
                                                cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
            glyphs.append(((labels[y:y + h, x:x + w] == i) * 255).astype(np.uint8))
        return glyphs

    @staticmethod
    def _score(glyph, tmpl):
        th, tw = tmpl.shape
        g = cv2.resize(glyph, (tw, th), interpolation=cv2.INTER_AREA)
        ar1, ar2 = glyph.shape[1] / glyph.shape[0], tw / th
        return _same_size_score(g, tmpl) * min(ar1, ar2) / max(ar1, ar2)

    def _best(self, glyph, digit=None):
        best_d, best = None, 0.0
        for d, tmpl in self.templates:
            if digit is not None and d != digit:
                continue
            s = self._score(glyph, tmpl)
            if s > best:
                best_d, best = d, s
        return best_d, best

    def read(self, roi_bgr):
        """L'entier lu, ou None si illisible."""
        if self.missing():
            return None  # calibrage incomplet : un chiffre inconnu serait pris pour un autre
        glyphs = self.segment(roi_bgr)
        if not glyphs:
            return None
        out = []
        for g in glyphs:
            d, score = self._best(g)
            if d is None or score < self.threshold:
                return None
            out.append(d)
        return int("".join(out))

    def learn(self, roi_bgr, value):
        """Apprend les chiffres de `value` ecrits dans la zone ; renvoie les chiffres ajoutes."""
        text = str(int(value))
        glyphs = self.segment(roi_bgr)
        if len(glyphs) != len(text):
            raise ValueError("%d caracteres trouves pour %d chiffres" % (len(glyphs), len(text)))
        os.makedirs(self.dir, exist_ok=True)
        added = []
        for g, d in zip(glyphs, text):
            if self._best(g, d)[1] >= 0.90:
                continue  # deja connu sous cette forme
            n = sum(1 for dd, _ in self.templates if dd == d)
            save_image(os.path.join(self.dir, "%s.png" % d if n == 0 else "%s_%d.png" % (d, n + 1)), g)
            self.templates.append((d, g))
            added.append(d)
        return added


# ------------------------------ Icones --------------------------------

class IconLibrary:
    """Icones de ressources nommees (icons_dir/<cle>.png + names_file {cle: nom}). Utilisable par plusieurs threads."""

    def __init__(self, icons_dir, names_file, threshold=0.85):
        self.dir, self.names_file, self.threshold = icons_dir, names_file, threshold
        self.lock = threading.Lock()
        self.names = {}
        self.images = {}
        try:
            with open(names_file, encoding="utf-8") as f:
                names = json.load(f)
        except (OSError, ValueError):
            names = {}
        for key, name in names.items():
            try:
                self.images[key] = load_template(self.path(key))
                self.names[key] = name
            except (OSError, ValueError):
                pass

    def path(self, key):
        return os.path.join(self.dir, key + ".png")

    def _save_names(self):
        tmp = self.names_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.names, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.names_file)

    def identify(self, icon_bgr):
        """(cle, nom, score) de l'icone la plus ressemblante ; (None, None, score) sous le seuil."""
        with self.lock:
            best_key, best = None, 0.0
            for key, img in self.images.items():
                h, w = img.shape[:2]
                score = _same_size_score(cv2.resize(icon_bgr, (w, h), interpolation=cv2.INTER_AREA), img)
                if score > best:
                    best_key, best = key, score
            if best_key is None or best < self.threshold:
                return None, None, best
            return best_key, self.names[best_key], best

    def add_unknown(self, icon_bgr):
        with self.lock:
            n = 1
            while "inconnue_%d" % n in self.names:
                n += 1
            key = "inconnue_%d" % n
            os.makedirs(self.dir, exist_ok=True)
            save_image(self.path(key), icon_bgr)
            self.images[key] = icon_bgr.copy()
            self.names[key] = key
            self._save_names()
            return key

    def rename(self, key, name):
        """Renomme ; renvoie l'ancien nom."""
        with self.lock:
            old = self.names[key]
            self.names[key] = name
            self._save_names()
            return old

    def delete(self, key):
        with self.lock:
            self.names.pop(key, None)
            self.images.pop(key, None)
            self._save_names()
            try:
                os.remove(self.path(key))
            except OSError:
                pass

    def items(self):
        with self.lock:
            return [(key, name, self.path(key)) for key, name in self.names.items()]


# --------------------------- Ecran de fin -----------------------------

TITLE_FILES = {"completed": "completed.png", "failed": "failed.png", "obtained": "obtained.png"}
DEFAULT_LAYOUT = {
    "slot_w": None, "slot_h": None, "title_threshold": 0.90, "icon_threshold": 0.85, "digit_threshold": 0.70,
    "text_min_value": 170, "text_max_saturation": 80,
}
SLOT_TOLERANCE = 0.10
SLOT_BORDER = 3       # pixels de cadre retires avant de decouper icone / nombre
ICON_PART = 0.65      # haut de la case = icone, le reste = nombre


def load_layout(stats_dir):
    layout = dict(DEFAULT_LAYOUT)
    try:
        with open(os.path.join(stats_dir, "layout.json"), encoding="utf-8") as f:
            layout.update(json.load(f))
    except (OSError, ValueError):
        pass
    return layout


def save_layout(stats_dir, layout):
    os.makedirs(stats_dir, exist_ok=True)
    path = os.path.join(stats_dir, "layout.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(layout, f, indent=1)
    os.replace(tmp, path)


class EndScreenReader:
    """Lit un ecran de fin : issue (titre) + cases OBTAINED (icone -> ressource, nombre)."""

    def __init__(self, stats_dir, icons, digits):
        self.dir, self.icons, self.digits = stats_dir, icons, digits
        self.titles = {}
        self.layout = dict(DEFAULT_LAYOUT)
        self.reload()

    def reload(self):
        self.layout = load_layout(self.dir)
        self.titles = {}
        for key, name in TITLE_FILES.items():
            try:
                self.titles[key] = load_template(os.path.join(self.dir, "titles", name))
            except (OSError, ValueError):
                pass
        self.icons.threshold = self.layout["icon_threshold"]
        self.digits.threshold = self.layout["digit_threshold"]
        self.digits.min_value = self.layout["text_min_value"]
        self.digits.max_saturation = self.layout["text_max_saturation"]

    def missing(self):
        out = []
        for key, text in (("completed", "titre reussi"), ("failed", "titre rate"), ("obtained", "en-tete OBTAINED")):
            if key not in self.titles:
                out.append(text)
        if not self.layout.get("slot_w") or not self.layout.get("slot_h"):
            out.append("taille de case")
        if self.digits.missing():
            out.append("chiffres")
        return out

    def outcome(self, screen):
        best, best_score = None, self.layout["title_threshold"]
        for key in ("completed", "failed"):
            tmpl = self.titles.get(key)
            if tmpl is None:
                continue
            score, center = match_image(screen, tmpl)
            if center is not None and score >= best_score:
                best, best_score = key, score
        return best

    def find_slots(self, screen):
        """Cases (x, y, w, h) sous l'en-tete OBTAINED, de gauche a droite."""
        tmpl = self.titles.get("obtained")
        sw, sh = self.layout.get("slot_w"), self.layout.get("slot_h")
        if tmpl is None or not sw or not sh:
            return []
        score, center = match_image(screen, tmpl)
        if center is None or score < self.layout["title_threshold"]:
            return []
        top = center[1] + tmpl.shape[0] - tmpl.shape[0] // 2
        bottom = min(screen.shape[0], top + int(1.6 * sh))
        band = screen[top:bottom]
        if band.size == 0:
            return []
        edges = cv2.Canny(cv2.cvtColor(band, cv2.COLOR_BGR2GRAY), 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        rects = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if abs(w - sw) <= sw * SLOT_TOLERANCE and abs(h - sh) <= sh * SLOT_TOLERANCE:
                rects.append((x, y + top, w, h))
        rects.sort(key=lambda r: -r[2] * r[3])  # le plus grand d'abord : il gagne les chevauchements
        kept = []
        for r in rects:
            if not any(_overlap(r, k) for k in kept):
                kept.append(r)
        return sorted(kept)

    @staticmethod
    def slot_parts(screen, rect):
        """(icone, zone du nombre) d'une case, cadre retire."""
        x, y, w, h = rect
        b = SLOT_BORDER
        inner = screen[y + b:y + h - b, x + b:x + w - b]
        split = int(inner.shape[0] * ICON_PART)
        return inner[:split].copy(), inner[split:].copy()

    def read(self, screen):
        """None s'il n'y a pas d'ecran de fin ; sinon {"outcome", "items": [...]}."""
        outcome = self.outcome(screen)
        if outcome is None:
            return None
        items = []
        for rect in self.find_slots(screen):
            icon, number = self.slot_parts(screen, rect)
            key, name, _ = self.icons.identify(icon)
            items.append({"key": key, "name": name, "amount": self.digits.read(number),
                          "icon": icon, "number": number})
        return {"outcome": outcome, "items": items}


def _overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


# ------------------------------ Session -------------------------------

CSV_FIELDS = ["debut", "fin", "duree", "reussies", "ratees"]


def format_duration(seconds):
    s = int(seconds)
    return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def format_amount(n):
    return "{:,}".format(int(n)).replace(",", " ")


class Session:
    """Compteurs d'une session. Les instants `now` sont des time.time() passes par l'appelant."""

    def __init__(self):
        self.started_at = None
        self.ended_at = None
        self.elapsed = 0.0          # secondes cumulees hors pause
        self.running_since = None   # None = en pause (ou pas demarree)
        self.completed = 0
        self.failed = 0
        self.totals = {}            # nom de ressource -> total
        self.last_mission = None
        self.current = "rien"
        self.previous = ""
        self._last_main = ""

    @property
    def running(self):
        return self.running_since is not None

    def start(self, now):
        self.started_at = now
        self.running_since = now

    def pause(self, now):
        if self.running_since is not None:
            self.elapsed += now - self.running_since
            self.running_since = None

    def resume(self, now):
        if self.running_since is None:
            self.running_since = now

    def duration(self, now):
        return self.elapsed + (now - self.running_since if self.running_since is not None else 0.0)

    def end(self, now):
        self.pause(now)
        self.ended_at = now

    def record(self, end_screen):
        if end_screen["outcome"] == "completed":
            self.completed += 1
        else:
            self.failed += 1
        for item in end_screen["items"]:
            if item.get("amount") is not None and item.get("name"):
                self.totals[item["name"]] = self.totals.get(item["name"], 0) + item["amount"]
        self.last_mission = end_screen

    def rename_resource(self, old, new):
        if old == new or old not in self.totals:
            return
        self.totals[new] = self.totals.get(new, 0) + self.totals.pop(old)

    def set_activity(self, main, detail):
        """main : nom du scenario / de la macro ("" si rien) ; detail : texte complet affiche."""
        self.current = detail
        if main and main != self._last_main:
            if self._last_main:
                self.previous = self._last_main
            self._last_main = main

    def should_write(self, now):
        return self.completed + self.failed > 0 or self.duration(now) >= 60

    def csv_row(self, now):
        end = self.ended_at if self.ended_at is not None else now
        row = {"debut": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started_at)),
               "fin": time.strftime("%Y-%m-%d %H:%M", time.localtime(end)),
               "duree": format_duration(self.duration(end)),
               "reussies": str(self.completed), "ratees": str(self.failed)}
        for name, total in self.totals.items():
            row[name] = str(total)
        return row

    def to_dict(self, now):
        return {"started_at": self.started_at, "elapsed": self.duration(now), "completed": self.completed,
                "failed": self.failed, "totals": dict(self.totals), "previous": self.previous,
                "last_main": self._last_main}

    @classmethod
    def from_dict(cls, d):
        s = cls()
        s.started_at = d["started_at"]
        s.elapsed = float(d["elapsed"])
        s.completed = int(d["completed"])
        s.failed = int(d["failed"])
        s.totals = {k: int(v) for k, v in d["totals"].items()}
        s.previous = d.get("previous", "")
        s._last_main = d.get("last_main", "")
        return s


# ----------------------- Anti-double-comptage -------------------------

def _signature(reading):
    return reading["outcome"], tuple((it.get("key"), it.get("name"), it.get("amount")) for it in reading["items"])


class EndScreenDetector:
    """Renvoie chaque ecran de fin une seule fois.

    read_fn(screen) -> lecture (dict) ou None s'il n'y a pas de titre de fin a l'ecran.
    libre -> vu (titre apparu) -> compte (2 lectures identiques, ou delai depasse, ou titre disparu)
    -> libre quand le titre est absent depuis gone_delay secondes.
    """

    def __init__(self, read_fn, stable_timeout=3.0, gone_delay=1.5, min_settle=1.0):
        self.read_fn, self.stable_timeout, self.gone_delay = read_fn, stable_timeout, gone_delay
        self.min_settle = min_settle  # laisse les cases / nombres finir leur animation d'apparition
        self.state = "free"
        self.last = None
        self.seen_at = 0.0
        self.gone_since = None

    def _emit(self, reading, unstable, gone_since=None):
        self.state = "counted"
        self.gone_since = gone_since
        result = dict(reading)
        result["unstable"] = unstable
        return result

    def feed(self, screen, now):
        r = self.read_fn(screen)
        if self.state == "free":
            if r is not None:
                self.state, self.seen_at, self.last = "seen", now, r
            return None
        if self.state == "seen":
            if r is None:  # ecran disparu avant d'avoir deux lectures identiques
                return self._emit(self.last, True, gone_since=now)
            if _signature(r) == _signature(self.last) and now - self.seen_at >= self.min_settle:
                return self._emit(r, False)
            self.last = r
            if now - self.seen_at >= self.stable_timeout:
                return self._emit(r, True)
            return None
        # compte : on attend que le titre ait disparu assez longtemps
        if r is not None:
            self.gone_since = None
        elif self.gone_since is None:
            self.gone_since = now
        elif now - self.gone_since >= self.gone_delay:
            self.state = "free"
        return None


def hud_lines(session, now):
    """Texte du HUD affiche par-dessus le jeu."""
    lines = ["⏱ %s%s" % (format_duration(session.duration(now)), "" if session.running else "  (en pause)"),
             "En cours : %s" % session.current,
             "Avant    : %s" % (session.previous or "-"),
             "Missions : ✔ %d   ✖ %d" % (session.completed, session.failed)]
    res = ["%s %s" % (name, format_amount(total)) for name, total in session.totals.items()]
    for i in range(0, len(res), 2):
        lines.append(" · ".join(res[i:i + 2]))
    return lines


# --------------------------- Fichiers (CSV) ----------------------------

def _read_csv(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            return list(reader.fieldnames or []), list(reader)
    except FileNotFoundError:
        return [], []


def append_session_csv(path, row):
    """Ajoute une session ; si une ressource n'a pas encore de colonne, l'en-tete est etendu."""
    fields, rows = _read_csv(path)
    for name in CSV_FIELDS + list(row):
        if name not in fields:
            fields.append(name)
    fields = CSV_FIELDS + [f for f in fields if f not in CSV_FIELDS]
    rows.append(row)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", restval="0")
        w.writeheader()
        for r in rows:
            w.writerow({k: (v if v not in (None, "") else "0") for k, v in r.items()})
    os.replace(tmp, path)


def read_history(path, limit=10):
    """Les `limit` dernieres sessions, la plus recente d'abord."""
    return list(reversed(_read_csv(path)[1]))[:limit]


def save_current(path, session, now):
    data = session.to_dict(now)
    data["saved_at"] = now
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def recover_current(path, csv_path):
    """Session restee en cours (plantage, fermeture brutale) : la cloture dans le CSV. True si recuperee."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        session = Session.from_dict(data)
        saved_at = float(data["saved_at"])
    except (OSError, ValueError, KeyError, TypeError):
        os.remove(path)
        return False
    session.end(saved_at)
    if session.should_write(saved_at):
        try:
            append_session_csv(csv_path, session.csv_row(saved_at))
        except OSError:
            return False  # CSV verrouille (ouvert dans Excel ?) : on reessaiera au prochain demarrage
    os.remove(path)
    return True
