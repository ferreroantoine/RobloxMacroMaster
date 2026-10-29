# Statistiques de session (missions, ressources, HUD) — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** compter les missions réussies / ratées et les ressources obtenues en lisant l'écran de fin, et afficher
l'état de la session (chrono, en cours, avant, compteurs) dans un HUD sur le jeu et un onglet « Session ».

**Architecture :** toute la logique (lecture d'image, anti-double-comptage, session, CSV) vit dans un nouveau module
`mission_stats.py` sans Tk, testé avec `unittest`. `macro_gui.py` ajoute un thread `MissionWatcher` (jamais mis en
pause par les scénarios), une fenêtre `Hud`, un onglet « Session » et un dialogue de calibrage.

**Tech Stack :** Python 3.12, tkinter, OpenCV (`cv2`), numpy, mss, Pillow — tous déjà utilisés. `unittest` (stdlib).

**Spec :** `docs/superpowers/specs/2026-10-29-mission-stats-design.md`

## Global Constraints

- Aucune nouvelle dépendance ; `build_exe.bat` inchangé.
- `roblox_macro.py`, `triggers.json`, `scenarios.json` inchangés. La fonctionnalité n'effectue jamais de clic.
- Résolution fixe : matching à échelle 1 (pas de multi-échelle).
- Données sous `stats/` relatif à `BASE` (cf. spec §3.3).
- Seuils par défaut : titre 0,90 ; icône 0,85 ; glyphe 0,70 ; nouvelle variante de chiffre si < 0,90 ; taille de case ±10 % ;
  icône = haut 65 % de la case ; nombre = bas 35 %.
- Détection : scan ~0,3 s ; stabilité = 2 lectures identiques consécutives ; délai max 3 s ; réarmement après 1,5 s sans titre.
- CSV : `stats/sessions.csv`, séparateur `;`, encodage `utf-8-sig`, colonnes `debut;fin;duree;reussies;ratees;<ressources...>`.
- Session non écrite dans le CSV si 0 mission **et** durée < 60 s.
- `session_en_cours.json` écrit après chaque mission et toutes les 60 s ; récupéré au démarrage.
- Réglages HUD : `hud` (True), `hud_corner` (`"haut-gauche"`), `hud_alpha` (0.8, borné 0.3–1.0).
- Textes d'interface en français, sans accents dans le code existant (suivre le style de `macro_gui.py`), journal préfixé `[STATS]`.
- Le dossier n'est pas un dépôt git : les étapes « Commit » sont remplacées par « Point de contrôle » (lancer toute la suite de tests).

## Review Focus

1. **Écran de fin visible un seul scan** (scénario « heal and retry » qui clique très vite) → la mission doit quand même être comptée une fois. Test : `test_detector_title_gone_after_single_read` (Tâche 2).
2. **Deux missions identiques d'affilée** (mêmes ressources, mêmes montants) séparées par un court écran sans titre → deux missions, pas une. Test : `test_detector_counts_two_identical_missions` (Tâche 2).
3. **Nombre avec virgules ou un seul chiffre** (« 166,807 », « 1 ») → lu 166807 et 1. Tests : `test_read_ignores_commas`, `test_read_single_digit` (Tâche 3).
4. **Icône inconnue revue à la mission suivante** → pas de doublon `inconnue_2` ; totaux fusionnés au renommage. Tests : `test_add_unknown_not_duplicated` (Tâche 4), `test_rename_resource_merges` (Tâche 1).
5. **Plantage / fermeture brutale** au milieu d'une session → la session apparaît dans le CSV au prochain démarrage. Test : `test_recover_current_appends_and_deletes` (Tâche 1).

---

## Structure des fichiers

| Fichier | Rôle |
|---|---|
| `mission_stats.py` (nouveau) | `Session`, fonctions CSV / formatage, `EndScreenDetector`, `DigitReader`, `IconLibrary`, `EndScreenReader`, `match_image`, `load_template`, `hud_lines` |
| `tests/__init__.py` (nouveau, vide) | rend `tests` importable |
| `tests/test_mission_stats.py` (nouveau) | tous les tests unitaires |
| `tests/fixtures/` (nouveau) | captures réelles plein écran (`completed_1.png`, `failed_1.png`) + `expected.json` |
| `macro_gui.py` (modifié) | `MissionWatcher`, `Hud`, `CalibrationDialog`, onglet Session, réglages HUD, cycle de vie |

Lancement des tests (depuis la racine du projet) : `python -m unittest tests.test_mission_stats -v`

---

### Tâche 1 : `Session`, CSV et formatage

**Files :**
- Create : `mission_stats.py`, `tests/__init__.py`, `tests/test_mission_stats.py`

**Interfaces :**
- Produces :
  - `format_duration(seconds: float) -> str` → `"1:23:45"` (heures non bornées, `"0:00:05"`).
  - `format_amount(n: int) -> str` → `"2 001 684"` (espace comme séparateur de milliers).
  - `class Session` : attributs `started_at: float`, `ended_at: float|None`, `completed: int`, `failed: int`,
    `totals: dict[str,int]`, `last_mission: dict|None`, `current: str`, `previous: str` ;
    méthodes `start(now)`, `pause(now)`, `resume(now)`, `running -> bool` (propriété), `duration(now) -> float`,
    `end(now)`, `record(end_screen: dict)`, `rename_resource(old: str, new: str)`,
    `set_activity(main: str, detail: str)`, `should_write(now) -> bool`, `csv_row(now) -> dict`,
    `to_dict(now) -> dict`, `Session.from_dict(d) -> Session` (classmethod).
  - `append_session_csv(path: str, row: dict) -> None`, `read_history(path: str, limit: int = 10) -> list[dict]`.
  - `save_current(path: str, session: Session, now: float) -> None`,
    `recover_current(path: str, csv_path: str) -> bool` (True si une session a été récupérée).
- `now` est toujours un horodatage `time.time()` passé par l'appelant (tests déterministes).

- [ ] **Step 1 : écrire les tests qui échouent**

```python
class SessionTest(unittest.TestCase):
    def test_duration_excludes_pauses(self):
        s = Session(); s.start(1000); s.pause(1100); s.resume(1500)
        self.assertEqual(s.duration(1600), 200)
        self.assertFalse(Session().running)

    def test_record_counts_and_totals(self):
        s = Session(); s.start(0)
        s.record({"outcome": "completed", "unstable": False, "items": [
            {"key": "gems", "name": "Gemmes", "amount": 199},
            {"key": "exp", "name": "EXP", "amount": None}]})
        s.record({"outcome": "failed", "unstable": False, "items": [
            {"key": "gems", "name": "Gemmes", "amount": 1}]})
        self.assertEqual((s.completed, s.failed), (1, 1))
        self.assertEqual(s.totals, {"Gemmes": 200})   # montant illisible ignoré

    def test_rename_resource_merges(self):
        s = Session(); s.totals = {"inconnue_1": 5, "Gemmes": 10}
        s.rename_resource("inconnue_1", "Gemmes")
        self.assertEqual(s.totals, {"Gemmes": 15})

    def test_set_activity_tracks_previous_main(self):
        s = Session()
        s.set_activity("farm", "macro farm")
        s.set_activity("", "rien")
        s.set_activity("heal and retry", "Scenario heal and retry 1/1 - etape 3/8 : ...")
        self.assertEqual(s.previous, "farm")
        self.assertTrue(s.current.startswith("Scenario heal and retry"))
        s.set_activity("heal and retry", "... etape 4/8")
        self.assertEqual(s.previous, "farm")

    def test_should_write(self):
        s = Session(); s.start(0)
        self.assertFalse(s.should_write(30))
        self.assertTrue(s.should_write(60))
        s2 = Session(); s2.start(0); s2.completed = 1
        self.assertTrue(s2.should_write(5))

    def test_dict_roundtrip(self):
        s = Session(); s.start(0); s.completed = 3; s.totals = {"EXP": 7}
        t = Session.from_dict(s.to_dict(50))
        self.assertEqual((t.completed, t.totals, t.duration(50)), (3, {"EXP": 7}, 50))

class CsvTest(unittest.TestCase):   # utilise tempfile.TemporaryDirectory
    def test_format(self):
        self.assertEqual(format_duration(5025), "1:23:45")
        self.assertEqual(format_amount(2001684), "2 001 684")

    def test_append_extends_header(self):
        # 1re ligne avec {"EXP": 10}, 2e avec {"Gemmes": 5} -> en-tête
        # "debut;fin;duree;reussies;ratees;EXP;Gemmes", 1re ligne Gemmes = "0"
        # fichier commence par le BOM utf-8 (b"\xef\xbb\xbf")

    def test_read_history_latest_first_limited(self):
        # 12 lignes ajoutées -> read_history(path, 10) renvoie 10 dicts, la plus récente d'abord

    def test_recover_current_appends_and_deletes(self):
        # save_current(cur, session avec 2 réussies, now=100) puis recover_current(cur, csv) -> True,
        # csv contient 1 ligne avec reussies == "2", cur n'existe plus ; 2e appel -> False
```

Les tests en commentaire sont à écrire avec exactement les assertions indiquées.

- [ ] **Step 2 : lancer — échec attendu** : `python -m unittest tests.test_mission_stats -v` → `ImportError` / `NameError`.

- [ ] **Step 3 : implémenter dans `mission_stats.py`**
  - `csv_row(now)` : `debut`/`fin` au format `"%Y-%m-%d %H:%M"` (fin = `ended_at` ou `now`), `duree` = `format_duration`,
    `reussies`/`ratees` en texte, puis `totals`.
  - `append_session_csv` : lit l'existant (`csv.DictReader`, `delimiter=";"`, `encoding="utf-8-sig"`), union des
    colonnes dans l'ordre fixe + ressources par ordre d'apparition, valeurs manquantes `"0"`, réécrit via `.tmp` + `os.replace`.
  - `save_current` écrit `to_dict(now)` + `"saved_at": now` en JSON (`.tmp` + `os.replace`). `recover_current` :
    `from_dict`, `end(saved_at)`, ajoute au CSV si `should_write(saved_at)`, supprime le fichier ; JSON illisible → supprimé, False.
  - `set_activity` : garde `_last_main` (dernier `main` non vide) ; si `main` non vide et différent → `previous = _last_main`.

- [ ] **Step 4 : lancer — tout passe.**
- [ ] **Step 5 : point de contrôle.**

---

### Tâche 2 : `EndScreenDetector` (anti-double-comptage)

**Files :** Modify `mission_stats.py` ; Test `tests/test_mission_stats.py`

**Interfaces :**
- Consumes : rien.
- Produces : `class EndScreenDetector(read_fn, stable_timeout=3.0, gone_delay=1.5)` ;
  `feed(screen, now: float) -> dict | None`. `read_fn(screen) -> dict | None` (None = pas de titre).
  Signature de comparaison d'une lecture : `(outcome, tuple((it["key"], it["name"], it["amount"]) for it in items))`.
  Le résultat émis est la lecture avec `"unstable"` mis à True/False.

- [ ] **Step 1 : tests qui échouent** — `read_fn` factice qui renvoie l'élément suivant d'une liste ; `now` avance de 0,3 :

```python
A = {"outcome": "completed", "items": [{"key": "g", "name": "G", "amount": 5}]}
B = {"outcome": "completed", "items": [{"key": "g", "name": "G", "amount": 9}]}

def run(frames, step=0.3):   # renvoie la liste des résultats émis
    ...

def test_detector_counts_once(self):
    out = run([None, A, A, A, A, None, None, None, None, None, None])
    self.assertEqual(len(out), 1); self.assertFalse(out[0]["unstable"])

def test_detector_waits_for_stable_reading(self):
    out = run([B, A, A])      # B pendant l'animation
    self.assertEqual(out[0]["items"][0]["amount"], 5)

def test_detector_unstable_after_timeout(self):
    out = run([A, B] * 6)     # jamais 2 identiques, > 3 s
    self.assertEqual(len(out), 1); self.assertTrue(out[0]["unstable"])

def test_detector_title_gone_after_single_read(self):
    out = run([None, A, None, None])
    self.assertEqual(len(out), 1); self.assertTrue(out[0]["unstable"])

def test_detector_counts_two_identical_missions(self):
    out = run([A, A] + [None] * 6 + [A, A])   # 6 × 0,3 = 1,8 s sans titre
    self.assertEqual(len(out), 2)

def test_detector_short_gap_does_not_rearm(self):
    out = run([A, A, None, None, A, A])       # 0,6 s sans titre < 1,5 s
    self.assertEqual(len(out), 1)
```

- [ ] **Step 2 : lancer — échec attendu.**
- [ ] **Step 3 : implémenter** la machine à états de la spec §4.2 (états `"free"`, `"seen"`, `"counted"` ; `gone_since` réinitialisé dès qu'un titre est revu).
- [ ] **Step 4 : lancer — tout passe.**
- [ ] **Step 5 : point de contrôle.**

---

### Tâche 3 : `DigitReader`

**Files :** Modify `mission_stats.py` ; Test `tests/test_mission_stats.py`

**Interfaces :**
- Produces : `class DigitReader(digits_dir: str, threshold=0.70, min_value=170, max_saturation=80)` ;
  `segment(roi_bgr) -> list[np.ndarray]` (masques binaires recadrés, gauche → droite, ponctuation retirée) ;
  `read(roi_bgr) -> int | None` ; `learn(roi_bgr, value: int) -> list[str]` (chiffres ajoutés ; `ValueError` si le
  nombre de glyphes ≠ `len(str(value))`) ; `missing() -> list[str]` ; `reload()`.
  Fichiers : `digits_dir/<d>.png`, variantes `<d>_2.png`, `<d>_3.png`…

- [ ] **Step 1 : tests qui échouent.** Helper `render(text, scale=0.5)` : image noire 30 px de haut, texte blanc
  `cv2.putText(..., cv2.FONT_HERSHEY_SIMPLEX, scale, (255,255,255), 1, cv2.LINE_8)`, marge 4 px.

```python
def test_learn_then_read(self):
    r = DigitReader(tmp)
    r.learn(render("1234567890"), 1234567890)
    self.assertEqual(r.missing(), [])
    self.assertEqual(r.read(render("907")), 907)

def test_read_ignores_commas(self):
    # après apprentissage : read(render("166,807")) == 166807

def test_read_single_digit(self):
    # read(render("1")) == 1

def test_missing_digits_reported(self):
    r = DigitReader(tmp); r.learn(render("123"), 123)
    self.assertEqual(r.missing(), list("0456789"))

def test_unknown_glyph_is_unreadable(self):
    # après apprentissage : read(render("12X4")) is None ; read(noir) is None

def test_learn_rejects_count_mismatch(self):
    with self.assertRaises(ValueError): DigitReader(tmp).learn(render("12"), 123)

def test_variant_only_if_different(self):
    # learn("11", 11) deux fois -> un seul fichier 1*.png dans tmp

def test_persisted_between_instances(self):
    # learn avec une instance, nouvelle instance sur le même dossier -> read fonctionne
```

- [ ] **Step 2 : lancer — échec attendu.**
- [ ] **Step 3 : implémenter.**
  - Masque texte : HSV, `V >= min_value` et `S <= max_saturation`.
  - `segment` : `cv2.connectedComponentsWithStats` (8-connexité) ; ignorer aire < 3 ; ignorer les composantes dont la
    hauteur < 50 % de la plus haute (virgules / points) ; trier par x ; recadrer chaque masque à sa boîte.
  - Score glyphe / modèle : redimensionner le glyphe à la taille du modèle, `cv2.matchTemplate(..., TM_CCOEFF_NORMED)`
    (valeur unique), multiplié par `min(ar1, ar2) / max(ar1, ar2)` (ar = largeur/hauteur) pour distinguer « 1 ».
    Glyphe uniforme (std 0) : score = 1 si le modèle l'est aussi, sinon 0.
  - `read` : None si aucun glyphe ou si un meilleur score < `threshold`.
  - `learn` : variante enregistrée seulement si le meilleur score avec les variantes existantes du même chiffre < 0,90.
- [ ] **Step 4 : lancer — tout passe.**
- [ ] **Step 5 : point de contrôle.**

---

### Tâche 4 : `IconLibrary`

**Files :** Modify `mission_stats.py` ; Test `tests/test_mission_stats.py`

**Interfaces :**
- Produces : `class IconLibrary(icons_dir: str, names_file: str, threshold=0.85)`, thread-safe (`threading.Lock`) ;
  `identify(icon_bgr) -> tuple[str|None, str|None, float]` (key, nom, meilleur score) ;
  `add_unknown(icon_bgr) -> str` (clé `inconnue_<n>`, nom identique, n = plus petit entier libre ≥ 1) ;
  `rename(key, name) -> str` (renvoie l'ancien nom) ; `delete(key)` ; `items() -> list[tuple[key, name, path]]`.
  Stockage : `icons_dir/<key>.png`, `names_file` = `{key: nom}` JSON.

- [ ] **Step 1 : tests qui échouent.** Helper `icon(seed)` : image 40×40 de motifs aléatoires (`np.random.default_rng(seed)`).

```python
def test_identify_known_after_rename(self):
    lib = IconLibrary(d, f); k = lib.add_unknown(icon(1)); lib.rename(k, "Gemmes")
    key, name, score = lib.identify(icon(1))
    self.assertEqual((key, name), (k, "Gemmes")); self.assertGreater(score, 0.99)

def test_identify_unknown(self):
    lib = IconLibrary(d, f); lib.add_unknown(icon(1))
    self.assertEqual(lib.identify(icon(2))[:2], (None, None))

def test_identify_tolerates_size_difference(self):
    # icône stockée 40x40, recherche avec cv2.resize(icon(1), (42, 41)) -> reconnue

def test_add_unknown_not_duplicated(self):
    # lib.add_unknown(icon(1)) ; puis identify(icon(1)) renvoie la clé inconnue_1 (pas None)

def test_names_persisted_and_delete(self):
    # rename puis nouvelle instance -> même nom ; delete(k) -> fichier png supprimé, absent de items()
```

- [ ] **Step 2 : lancer — échec attendu.**
- [ ] **Step 3 : implémenter** (comparaison : redimensionner l'entrée à la taille de l'icône stockée, `TM_CCOEFF_NORMED`).
- [ ] **Step 4 : lancer — tout passe.**
- [ ] **Step 5 : point de contrôle.**

---

### Tâche 5 : `EndScreenReader` + déplacement de `match_image` / `load_template`

**Files :** Modify `mission_stats.py`, `macro_gui.py:52-60,297-320` ; Test `tests/test_mission_stats.py`

**Interfaces :**
- Consumes : `DigitReader`, `IconLibrary`.
- Produces :
  - `match_image(screen, tmpl) -> (float, (x, y) | None)` et `load_template(path) -> ndarray` : **déplacés tels quels**
    depuis `macro_gui.py` ; `macro_gui.py` les importe dans son bloc `try` des imports vision
    (`from mission_stats import match_image, load_template`) et supprime ses copies.
  - `load_layout(stats_dir) -> dict` / `save_layout(stats_dir, layout)` ; défauts
    `{"slot_w": None, "slot_h": None, "title_threshold": 0.90, "icon_threshold": 0.85, "digit_threshold": 0.70,
    "text_min_value": 170, "text_max_saturation": 80}`.
  - `class EndScreenReader(stats_dir, icons: IconLibrary, digits: DigitReader)` ;
    `reload()` (relit `titles/*.png` et `layout.json`) ; `missing() -> list[str]` parmi
    `"titre reussi"`, `"titre rate"`, `"en-tete OBTAINED"`, `"taille de case"`, `"chiffres"` ;
    `outcome(screen) -> "completed" | "failed" | None` ; `find_slots(screen) -> list[(x, y, w, h)]` ;
    `read(screen) -> dict | None`. Chaque item : `{"key", "name", "amount", "icon": ndarray, "number": ndarray}` ;
    `key`/`name` à None si icône inconnue.
  - Constantes : `TITLE_FILES = {"completed": "completed.png", "failed": "failed.png", "obtained": "obtained.png"}`.

- [ ] **Step 1 : tests qui échouent.** Écran synthétique 1280×720 gris foncé : on colle un titre (texte « MISSION
  COMPLETED » dessiné sur une petite image, aussi enregistrée comme `titles/completed.png`), un en-tête « OBTAINED »,
  puis 3 cases 60×60 (cadre 2 px de couleurs différentes, motif `icon(seed)` dans le haut, nombre `render(...)` en bas à droite).

```python
def test_outcome(self):        # completed détecté ; écran vide -> None ; failed.png collé -> "failed"
def test_find_slots(self):     # layout slot 60x60 -> 3 cases triées par x ; une case 90x90 ajoutée est ignorée
def test_read_full(self):      # icônes 1 et 2 nommées, chiffres appris -> outcome "completed",
                               # amounts [32054, 199, 1], icône 3 -> key None
def test_read_without_obtained(self):   # pas d'en-tête -> items == []
def test_missing(self):        # dossier vide -> les 5 éléments ; tout configuré -> []

@unittest.skipUnless(os.path.exists(FIXTURES / "expected.json"), "captures reelles absentes")
def test_real_screens(self):   # pour chaque entrée de expected.json : outcome et amounts attendus
```

- [ ] **Step 2 : lancer — échec attendu.**
- [ ] **Step 3 : implémenter** selon la spec §4.1–4.4. Bande de recherche sous l'en-tête : de `y_bas_entete` à
  `+ 1,6 × slot_h`, largeur de l'écran. Cases : `cv2.Canny` sur niveaux de gris, `findContours(RETR_LIST)`,
  `boundingRect`, taille dans ±10 % de (slot_w, slot_h), dédoublonnage par chevauchement (garder le plus grand).
  Icône = `slot[: int(0.65*h)]`, nombre = `slot[int(0.65*h):]`, en retirant 3 px de cadre.
  Si les chiffres ou la taille de case manquent : items avec `amount` None (ou `items == []` sans taille de case).
- [ ] **Step 4 : lancer** — tout passe, et `python -c "import macro_gui"` ne lève rien.
- [ ] **Step 5 : point de contrôle.**

---

### Tâche 6 : `hud_lines` (texte du HUD)

**Files :** Modify `mission_stats.py` ; Test `tests/test_mission_stats.py`

**Interfaces :** Produces `hud_lines(session: Session, now: float) -> list[str]`.

- [ ] **Step 1 : test qui échoue**

```python
def test_hud_lines(self):
    s = Session(); s.start(0); s.completed, s.failed = 12, 2
    s.totals = {"EXP": 384648, "Gemmes": 2388}
    s.set_activity("farm", "macro farm"); s.set_activity("heal and retry", "Scenario heal and retry - etape 3/8")
    self.assertEqual(hud_lines(s, 5025), [
        "⏱ 1:23:45",
        "En cours : Scenario heal and retry - etape 3/8",
        "Avant    : farm",
        "Missions : ✔ 12   ✖ 2",
        "EXP 384 648 · Gemmes 2 388"])
    s.pause(5025)
    self.assertEqual(hud_lines(s, 9999)[0], "⏱ 1:23:45  (en pause)")
```
  Ressources : 2 par ligne maximum (plusieurs lignes au-delà) ; aucune ressource → pas de ligne ressources ;
  `previous` vide → `"Avant    : -"`.

- [ ] **Step 2–4 : échec, implémentation, succès.**
- [ ] **Step 5 : point de contrôle.**

---

### Tâche 7 : `MissionWatcher` et cycle de vie de la session dans `macro_gui.py`

**Files :** Modify `macro_gui.py` (constantes ~l.66-76, `__init__` ~l.917-951, `_handle` ~l.1233, `_update_live` ~l.1718,
`toggle_watch` ~l.1889, `close` ~l.2054)

**Interfaces :**
- Consumes : tout `mission_stats`.
- Produces (utilisé par les tâches 8–10) :
  - Constantes `STATS_DIR = os.path.join(BASE, "stats")`, `SESSIONS_CSV`, `CURRENT_SESSION` (`stats/session_en_cours.json`).
  - Attributs `Gui` : `session: Session | None`, `mission_watcher`, `icons: IconLibrary`, `digits: DigitReader`,
    `reader: EndScreenReader` (ces trois à None si `VISION_ERROR`), `last_save: float`.
  - `Gui.current_activity() -> tuple[str, str]` (main, detail) extrait de `_update_live` (même texte que `running_label`) ;
    main = nom du scénario / de la macro / `"enregistrement"` / `""`.
  - `Gui.end_session()`, `Gui.on_mission(result: dict)`, `Gui.start_mission_watch()`, `Gui.stop_mission_watch()`.
  - Message de file : `("mission", result)` et `("mission_watch_stopped",)`.
  - Hook de rafraîchissement : `Gui.refresh_session_views()` (vide ici, rempli par les tâches 8 et 9).

- [ ] **Step 1 : `class MissionWatcher(threading.Thread)`** à côté de `Watcher` : `__init__(gui)`, `stop()`, `run()` —
  boucle `grab_screen` → `EndScreenDetector(gui.reader.read).feed(screen, time.monotonic())` → `gui.post("mission", r)` ;
  attente `stop_evt.wait(0.3)` ; exception → `rm.log("[!] Compteur de missions arrete :\n" + traceback)` + post
  `("mission_watch_stopped",)`. Ne consulte jamais `engine.busy()`.
- [ ] **Step 2 : démarrage** — dans `Gui.__init__` après le chargement des réglages : `os.makedirs(STATS_DIR)`,
  `recover_current(CURRENT_SESSION, SESSIONS_CSV)` (log `[STATS] Session precedente recuperee dans sessions.csv.` si True),
  création de `icons`, `digits`, `reader` si pas `VISION_ERROR`.
- [ ] **Step 3 : `toggle_watch`** — activation : `session = Session(); start(now)` si None, sinon `resume(now)` ;
  `start_mission_watch()` si `reader.missing()` ne contient ni titre réussi ni titre raté, sinon log
  `[STATS] Compteur de missions inactif : il manque <liste>.`. Désactivation : `session.pause(now)`, `stop_mission_watch()`,
  `save_current`.
- [ ] **Step 4 : `on_mission(result)`** — pour chaque item sans clé : `icons.identify` puis sinon `icons.add_unknown` ;
  `amount` None → sauver `item["number"]` dans `stats/illisibles/<YYYYmmdd_HHMMSS>_<i>.png`, **sauf** si
  `"chiffres"` ou `"taille de case"` est dans `reader.missing()` : dans ce cas un seul log par session
  `[STATS] Chiffres non calibres : ressources ignorees (onglet Session > Calibrer).` ; `session.record` ;
  `save_current` ; log `[STATS] Mission reussie : EXP 32 054, Gemmes 199` (ou `ratee`, `illisible` pour les montants
  manquants, ` (lecture instable)` si `unstable`) ; `refresh_session_views()`. Ignoré si `session` est None.
- [ ] **Step 5 : `end_session()`** — `session.end(now)`, `append_session_csv` si `should_write`, suppression de
  `CURRENT_SESSION`, log `[STATS] Session terminee : <duree>, <n> reussies, <m> ratees.`, `session = None` ; si la
  surveillance est encore active, une nouvelle session démarre aussitôt. `close()` appelle `end_session()` en premier
  et `stop_mission_watch()`.
- [ ] **Step 6 : `_refresh_live`** — `session.set_activity(*current_activity())`, `save_current` si
  `now - last_save >= 60`, `refresh_session_views()`.
- [ ] **Step 7 : vérification manuelle** — `python macro_gui.py` ; activer / désactiver la surveillance trois fois ;
  journal sans traceback ; `stats/session_en_cours.json` présent ; fermer l'app → `stats/sessions.csv` absent
  (session < 60 s sans mission) et `session_en_cours.json` supprimé. Lancer `python -m unittest tests.test_mission_stats -v` → tout passe.
- [ ] **Step 8 : point de contrôle.**

---

### Tâche 8 : HUD + réglages

**Files :** Modify `macro_gui.py` (`DEFAULT_SETTINGS` l.72, `_build_settings` l.1186, `save_settings` l.2033, `apply_settings` l.2026)

**Interfaces :**
- Consumes : `hud_lines`, `Gui.refresh_session_views`.
- Produces : `class Hud(tk.Toplevel)` : `__init__(gui)`, `update_lines(lines: list[str])`, `apply_settings(corner, alpha)`.

- [ ] **Step 1 : `DEFAULT_SETTINGS`** += `"hud": True, "hud_corner": "haut-gauche", "hud_alpha": 0.8`.
- [ ] **Step 2 : `Hud`** — `overrideredirect(True)`, `attributes("-topmost", True, "-alpha", alpha)`, fond `#202020`,
  `tk.Label` texte `#f0f0f0`, police `("Segoe UI", 10)`, `justify="left"`. Après `update_idletasks`, styles étendus via
  `hwnd = ctypes.windll.user32.GetParent(self.winfo_id())` et `SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80)`
  (`WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW`). Position : coin de l'écran principal avec marge `px(12)`,
  recalculée à chaque `update_lines` (la hauteur change).
- [ ] **Step 3 : affichage** — dans `refresh_session_views` : HUD visible si `cfg.hud` et `session` non None, contenu
  `hud_lines(session, time.time())` ; sinon `withdraw()`.
- [ ] **Step 4 : réglages** — case « Afficher le HUD sur le jeu », `ttk.Combobox` (`readonly`) des 4 coins, champ
  « Opacite du HUD (0.3 a 1) » ; `save_settings` valide `0.3 <= hud_alpha <= 1.0` (sinon `messagebox.showerror`) ;
  `apply_settings` appelle `hud.apply_settings`.
- [ ] **Step 5 : vérification manuelle** — surveillance activée avec Roblox ouvert : HUD dans le coin choisi, chrono qui
  avance, clics qui traversent vers le jeu, pas d'icône dans la barre des tâches ; changement de coin / opacité pris en
  compte dès « Enregistrer les reglages » ; décocher → HUD masqué.
- [ ] **Step 6 : point de contrôle.**

---

### Tâche 9 : Onglet « Session »

**Files :** Modify `macro_gui.py` (`_build` l.994 : ajouter `("session", "Session", self._build_session)` après « Actif »)

**Interfaces :**
- Consumes : `Gui.session`, `Gui.end_session`, `Gui.icons`, `Gui.reader`, `read_history`, `format_*`, `ImagePicker`, `Gui.thumb`.
- Produces : `Gui._build_session(parent)`, complète `Gui.refresh_session_views()` ; bouton « Calibrer les chiffres... »
  appelant `Gui.open_calibration()` (défini en tâche 10 ; d'ici là, un stub qui logge `Calibrage : bientot`).

- [ ] **Step 1 : bloc en-tête** — libellés durée, état (`en cours` / `en pause` / `aucune session`), réussies, ratées,
  taux (`"%d %%"`, `-` si 0 mission), bouton « Terminer la session » (confirmation `askyesno`).
- [ ] **Step 2 : ressources** — `Treeview` colonnes Ressource | Total | Par heure (`total * 3600 / duration`, `-` si durée < 60 s).
- [ ] **Step 3 : dernière mission** — texte `Reussie` / `Ratee` (+ ` - lecture instable`) et une ligne par item
  `nom : montant` (`illisible` si None).
- [ ] **Step 4 : lecture** — trois `ImagePicker(frame, self, self.root, value)` (titre réussi, titre raté, OBTAINED) ;
  `picker.var.trace_add("write", ...)` copie l'image choisie vers `stats/titles/<fichier de TITLE_FILES>` puis `reader.reload()`.
  Liste des icônes (`Treeview` style `Thumb.Treeview`, miniature via `self.thumb(path)`, inconnues tagguées en orange) avec
  boutons « Renommer... » (`simpledialog.askstring` ; puis `session.rename_resource(ancien, nouveau)` si session) et
  « Supprimer ». Texte `Il manque : <reader.missing()>` en rouge, ou `Lecture prete.` en vert.
- [ ] **Step 5 : historique** — `Treeview` des 10 dernières lignes de `read_history` (début, durée, réussies, ratées,
  puis les ressources concaténées `EXP 384 648 · ...`), rafraîchi à l'ouverture de l'onglet et après `end_session`.
- [ ] **Step 6 : `refresh_session_views`** ne met à jour l'onglet que s'il est l'onglet visible (`current_page() == "session"`),
  le HUD toujours. Si `VISION_ERROR` : l'onglet n'affiche que le message d'installation (comme `_build_actif`).
- [ ] **Step 7 : vérification manuelle** — choisir une image pour chaque titre : fichiers présents dans `stats/titles/`,
  message « Il manque » mis à jour ; « Terminer la session » après une session > 60 s → ligne dans l'historique et dans
  `stats/sessions.csv` ouvert dans Excel (colonnes séparées, accents corrects).
- [ ] **Step 8 : point de contrôle.**

---

### Tâche 10 : Dialogue de calibrage

**Files :** Modify `macro_gui.py` (nouvelle classe `CalibrationDialog(tk.Toplevel)` près de `TriggerDialog`)

**Interfaces :**
- Consumes : `EndScreenReader.find_slots`, `DigitReader.learn/missing`, `save_layout/load_layout`, `Gui.capture_region`.
- Produces : `Gui.open_calibration()` ; `CalibrationDialog(gui, screen_bgr)`.

- [ ] **Step 1 : source** — `open_calibration` propose « Capturer l'ecran » (cache la fenêtre, compte à rebours
  `cfg.countdown`, `grab_screen`, comme `test_trigger`) ou « Choisir une image... » (`load_template`).
- [ ] **Step 2 : taille de case** — si `slot_w` absent ou bouton « Redefinir la taille des cases » : `capture_region` ;
  la taille de l'image renvoyée devient `slot_w`/`slot_h` (`save_layout`), puis le fichier capturé est supprimé.
- [ ] **Step 3 : saisie** — pour chaque case de `find_slots(screen)` : zone nombre agrandie ×4 (`PIL`, `NEAREST`) +
  `ttk.Entry`. Aucune case trouvée → message `Aucune case trouvee : verifie l'en-tete OBTAINED et la taille des cases.`
- [ ] **Step 4 : apprentissage** — « Apprendre » : pour chaque champ non vide, `digits.learn(zone, int(texte))` ;
  `ValueError` → ligne du résultat `Case n : chiffres mal separes, ignoree` ; afficher les chiffres appris et
  `Il manque encore : 4, 8` (ou `Tous les chiffres sont appris.`). Champ non numérique → ignoré avec message.
- [ ] **Step 5 : vérification manuelle** — avec un écran de fin réel : calibrage, puis `reader.read` sur la même capture
  (bouton « Tester la lecture » du dialogue, qui affiche issue + `nom : montant` par case) → montants identiques à l'écran.
- [ ] **Step 6 : point de contrôle.**

---

### Tâche 11 : Validation sur captures réelles

**Files :** Create `tests/fixtures/completed_1.png`, `tests/fixtures/failed_1.png`, `tests/fixtures/expected.json` ;
peut modifier les défauts de `load_layout` (`text_min_value`, `text_max_saturation`) si les vraies captures l'exigent.

**Interfaces :** `expected.json` = `[{"file": "completed_1.png", "outcome": "completed", "amounts": [32054, 166807, 199, 1, 1]}, ...]`.
Les fixtures utilisent le dossier `stats/` de test copié dans `tests/fixtures/stats/` (titres, layout, chiffres, icônes calibrés).

- [ ] **Step 1 :** obtenir les captures plein écran de l'utilisateur ; calibrer avec la tâche 10 ; copier `stats/titles`,
  `stats/digits`, `stats/icons*`, `stats/layout.json` vers `tests/fixtures/stats/`.
- [ ] **Step 2 :** `python -m unittest tests.test_mission_stats -v` → `test_real_screens` exécuté (non `skipped`) et réussi.
  En cas d'échec, ajuster les seuils de binarisation dans `load_layout` uniquement, puis relancer.
- [ ] **Step 3 : vérification en jeu** — session de ≥ 3 missions avec la macro : HUD et onglet comptent exactement le
  nombre de missions jouées, totaux = somme des écrans ; « Terminer la session » → ligne correcte dans `sessions.csv`.
- [ ] **Step 4 : point de contrôle final** — suite complète verte ; `build_exe.bat` puis lancement de `MacroRoblox.exe` : onglet Session présent.
