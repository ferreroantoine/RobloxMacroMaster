# Statistiques de session : missions, ressources, HUD — Design

Date : 2026-10-29
Statut : en relecture

## 1. Objectif

Pendant que la macro farme des missions, l'utilisateur veut savoir en direct et en fin de session :

- depuis combien de temps la session tourne ;
- ce qui se joue actuellement et ce qui s'est joué juste avant ;
- combien de missions ont été réussies (« MISSION COMPLETED ») et ratées (« MISSION FAILED ») ;
- le total de chaque ressource obtenue (cases de la section « OBTAINED » de l'écran de fin).

Critère de succès : après une session de 2 h, l'utilisateur lit la durée, le nombre de missions réussies / ratées
et le total par ressource, et retrouve ce bilan dans un historique.

## 2. Décisions validées

| Sujet | Décision |
|---|---|
| Affichage | HUD compact en surimpression sur le jeu **et** onglet « Session » détaillé dans l'app |
| Persistance | Une ligne par session ajoutée à `stats/sessions.csv` ; 10 dernières affichées dans l'onglet |
| Bornes de session | Démarre avec la surveillance ; couper la surveillance met le chrono en pause ; la session se termine par le bouton « Terminer la session » ou à la fermeture de l'app |
| Résolution | Fixe (même écran, même taille de Roblox) : matching à échelle 1, pas de multi-échelle |
| Lecture | Modèles appris dans le jeu (icônes + chiffres 0-9), pas d'OCR externe, aucune nouvelle dépendance |

## 3. Architecture

### 3.1 Nouveau module `mission_stats.py` (logique pure, sans Tk)

Dépend uniquement de `numpy` et `cv2` (déjà requis par `macro_gui.py`) et de la bibliothèque standard.

| Unité | Responsabilité | Interface principale |
|---|---|---|
| `DigitReader` | Lit un entier dans une zone d'image ; apprend des glyphes | `read(roi_bgr) -> int \| None` ; `learn(roi_bgr, value: int) -> list[str]` (chiffres appris) ; `missing() -> list[str]` |
| `IconLibrary` | Icônes de ressources nommées, stockées dans `stats/icons/` + `stats/icons.json` | `identify(icon_bgr) -> (key, name, score)` ; `add_unknown(icon_bgr) -> key` ; `rename(key, name)` ; `delete(key)` |
| `EndScreenReader` | Analyse une capture d'écran complète | `read(screen_bgr) -> EndScreen \| None` |
| `EndScreenDetector` | Machine à états anti-double-comptage | `feed(screen_bgr, now) -> EndScreen \| None` (renvoie un résultat une seule fois par écran de fin) |
| `Session` | Compteurs, chrono avec pauses, activité courante / précédente, sérialisation | voir 3.4 |

Structures de données :

```python
EndScreen = {
    "outcome": "completed" | "failed",
    "items": [ {"key": str, "name": str, "amount": int | None, "icon": ndarray}, ... ],
    "unstable": bool,   # lecture jamais stabilisée avant le délai
}
```

### 3.2 Ajouts dans `macro_gui.py`

- **`MissionWatcher(threading.Thread)`** : scanne l'écran toutes les ~0,3 s avec son propre `screen_grabber()`.
  Il **ne se met pas en pause** pendant les macros / scénarios (contrairement au `Watcher` existant), car le
  déclencheur « retry » lance un scénario précisément sur l'écran de fin. Il n'envoie que des messages
  `self.gui.post("mission", end_screen)` ; il ne clique jamais et ne modifie pas la `Session`.
  Il tourne tant que la surveillance est active et que les images de titre sont configurées.
- **`Hud(tk.Toplevel)`** : fenêtre sans bordure, `-topmost`, `-alpha` réglable, styles Win32
  `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW` (clics traversants, absente de la barre des tâches),
  placée dans un coin choisi de l'écran principal.
- **Onglet « Session »** (`_build_session`), ajouté au `Notebook` après « Actif ».
- **Rafraîchissement** : le HUD et l'onglet sont mis à jour dans `_refresh_live` (toutes les 500 ms).
- **Message `"mission"`** traité dans `_handle` : appelle `session.record(end_screen)`, journalise, rafraîchit.

### 3.3 Données (`stats/`, relatif à `BASE`)

```
stats/
  titles/completed.png      titre « MISSION COMPLETED »
  titles/failed.png         titre « MISSION FAILED »
  titles/obtained.png       en-tête « OBTAINED »
  layout.json               taille de case apprise au calibrage, seuils
  digits/0.png .. 9.png     glyphes appris (plusieurs variantes possibles : 7_1.png, ...)
  icons/<key>.png           icônes
  icons.json                {key: nom}
  illisibles/<horodatage>_<n>.png   zones de nombres non lues
  sessions.csv              historique
  session_en_cours.json     sauvegarde de la session active
```

### 3.4 `Session`

État : `started_at`, `ended_at`, `elapsed` (secondes cumulées hors pause), `running_since` (ou `None` si en pause),
`completed`, `failed`, `totals: {nom: int}`, `last_mission: EndScreen`, `current: str`, `previous: str`.

Méthodes :

- `start(now)`, `pause(now)`, `resume(now)`, `duration(now) -> float`, `end(now)` ;
- `record(end_screen)` : +1 réussie / ratée ; ajoute chaque `amount` non `None` à `totals[name]` ;
- `rename_resource(old, new)` : fusionne `totals[old]` dans `totals[new]` (utilisé quand une icône inconnue est nommée) ;
- `set_activity(text)` : si l'activité « principale » (nom du scénario ou de la macro, pas l'étape) change,
  l'ancienne devient `previous` ; `current` garde le texte complet (scénario + étape) ;
- `to_dict()` / `from_dict()` (sauvegarde JSON) ; `csv_row() -> dict`.

Activité courante : même source que `running_label` de l'onglet Actif (`runner.current`, `player.name`,
enregistrement). Activité principale = nom du scénario ou de la macro.

Cycle de vie :

- activation de la surveillance : si aucune session → `start`, sinon `resume` ;
- désactivation de la surveillance : `pause` ;
- bouton « Terminer la session » ou fermeture de l'app : `end` → ligne CSV → suppression de `session_en_cours.json` ;
- sessions sans aucune mission et de moins d'une minute : non écrites dans le CSV.

## 4. Détection et lecture de l'écran de fin

### 4.1 Titres

Matching des modèles `completed.png` et `failed.png` sur l'écran entier (`match_image` existant, déplacé ou
réimporté). Seuil par défaut 0,90, réglable dans `layout.json`. Si les deux dépassent le seuil, le meilleur score gagne.

### 4.2 Anti-double-comptage (`EndScreenDetector`)

| État | Condition | Transition |
|---|---|---|
| LIBRE | titre vu | → VU (mémorise l'heure) |
| VU | deux lectures consécutives identiques (même issue, mêmes clés, mêmes montants) | émet le résultat → COMPTÉ |
| VU | 3 s écoulées sans stabilité | émet la dernière lecture avec `unstable=True` → COMPTÉ |
| VU | titre disparu avant stabilité | émet la dernière lecture avec `unstable=True` → COMPTÉ (une mission vue n'est jamais perdue) |
| COMPTÉ | titre absent pendant ≥ 1,5 s d'affilée | → LIBRE |

### 4.3 Cases OBTAINED

1. Localiser `obtained.png` ; s'il est absent, l'écran est traité sans ressources (`items = []`).
2. Zone de recherche : bande horizontale sous l'en-tête, de hauteur `1,6 × taille de case`, sur la largeur du panneau.
3. Détection des cases par les bords (Canny + contours approximés en quadrilatères) ; on ne garde que les
   rectangles de la taille apprise à ±10 %, triés de gauche à droite, sans chevauchement.
   La couleur du cadre n'est pas utilisée (elle varie selon la rareté).

### 4.4 Icône

Zone : intérieur de la case, partie haute (~65 %). Comparaison `TM_CCOEFF_NORMED` à taille égale avec chaque icône
connue. Score ≥ 0,85 → reconnue. Sinon → enregistrée comme `inconnue_<n>` et comptée sous ce nom ;
quand l'utilisateur la nomme, `Session.rename_resource` fusionne le total. Une icône inconnue déjà vue
(score ≥ 0,85 avec une icône `inconnue_*`) n'est pas dupliquée.

### 4.5 Nombre (`DigitReader`)

1. Zone : bas de la case (~35 %).
2. Binarisation : pixels clairs et peu saturés (texte blanc / gris) ; seuils dans `layout.json`.
3. Découpage : composantes connexes, triées de gauche à droite ; les petites taches basses (virgules, points) sont ignorées.
4. Chaque glyphe est mis à la hauteur des modèles et comparé à toutes les variantes 0-9 ; score de la meilleure.
5. Si un glyphe a un score < 0,70 ou si aucun glyphe n'est trouvé → `None` (illisible) : la zone est sauvegardée
   dans `stats/illisibles/` et un message part dans le journal.

### 4.6 Calibrage (dialogue « Calibrer… »)

1. Source : capture de l'écran actuel (avec le compte à rebours existant) ou image choisie sur le disque.
2. Si la taille de case est inconnue : l'utilisateur entoure une case (`RegionSelector` existant) → taille enregistrée.
3. Les cases détectées sont affichées avec leur zone de nombre agrandie ; l'utilisateur tape chaque nombre.
4. `DigitReader.learn` découpe les glyphes ; si le nombre de glyphes ≠ nombre de chiffres tapés, la case est
   ignorée avec un message. Chaque chiffre nouveau est enregistré ; un chiffre existant n'est ajouté comme variante
   que s'il ressemble à moins de 0,90 aux variantes existantes.
5. Le dialogue affiche les chiffres encore manquants.

### 4.7 Mission ratée

Même lecture de la section OBTAINED si elle est présente. À confirmer avec une capture réelle de l'écran FAILED.

## 5. Interface

### 5.1 HUD

```
⏱ 1:23:45                (« en pause » si surveillance coupée)
En cours : heal and retry – étape 3/8
Avant    : farm
Missions : ✔ 12   ✖ 2
EXP 384 648 · Pièces 2 001 684 · Gemmes 2 388
```

Fond sombre, texte clair, Segoe UI 10, largeur ~300 px (adaptée au DPI via `px()`), ressources sur plusieurs
lignes si nécessaire. Masqué s'il n'y a pas de session. Nouveaux réglages (`DEFAULT_SETTINGS`) :
`hud` (bool, défaut `True`), `hud_corner` (`"haut-gauche"` | `"haut-droite"` | `"bas-gauche"` | `"bas-droite"`,
défaut `"haut-gauche"`), `hud_alpha` (0,3 à 1,0, défaut 0,8).

### 5.2 Onglet « Session »

- **En-tête** : durée, état (en cours / en pause / aucune session), réussies, ratées, taux de réussite,
  bouton « Terminer la session ».
- **Ressources** : `Treeview` Ressource | Total | Par heure.
- **Dernière mission** : issue, miniatures + montants lus (« illisible » le cas échéant).
- **Lecture** : trois `ImagePicker` (titre réussi, titre raté, OBTAINED), bouton « Calibrer les chiffres… » avec
  la liste des chiffres manquants, liste des icônes (miniature, nom ; renommer, supprimer ; inconnues en évidence).
- **Historique** : 10 dernières lignes de `sessions.csv`.

## 6. Persistance

- `sessions.csv` : séparateur `;`, encodage `utf-8-sig`. Colonnes : `debut`, `fin`, `duree` (h:mm:ss),
  `reussies`, `ratees`, puis une colonne par ressource. Si une ressource n'a pas encore de colonne, le fichier est
  relu et réécrit avec l'en-tête étendu (cases vides = 0). Écriture via fichier temporaire + `os.replace`.
- `session_en_cours.json` : écrit après chaque mission et toutes les 60 s. Au démarrage, s'il existe, la session
  est clôturée (fin = dernière sauvegarde) et ajoutée au CSV, puis le fichier est supprimé.

## 7. Erreurs

| Cas | Comportement |
|---|---|
| OpenCV / mss absents (`VISION_ERROR`) | Onglet Session affiche le message d'installation ; pas de `MissionWatcher` |
| Titres non configurés | `MissionWatcher` non lancé ; l'onglet indique quelles images manquent |
| Taille de case / chiffres non calibrés | Missions comptées, ressources ignorées ; message unique dans le journal |
| Nombre illisible | Mission comptée, ressource non ajoutée, zone sauvegardée, message journal |
| Icône inconnue | Comptée sous `inconnue_<n>`, à nommer dans l'onglet |
| Exception dans `MissionWatcher` | Traceback dans le journal, thread arrêté, état visible dans l'onglet |

## 8. Threads

`MissionWatcher` lit l'écran et les modèles (lecture seule) et poste des messages dans `gui.q`.
`Session` et le CSV ne sont modifiés que dans le thread Tk. `IconLibrary` est partagée et protégée par un verrou :
le watcher ne fait qu'`identify` ; les icônes inconnues sont renvoyées avec `key = None` et c'est le thread Tk qui
les enregistre (`add_unknown`) à la réception du message, après un nouvel `identify` pour éviter les doublons.

## 9. Tests

`tests/test_mission_stats.py` (`unittest`, bibliothèque standard), lancés par `python -m unittest`.

- `Session` : chrono avec pauses ; totaux ; `rename_resource` ; `csv_row` ; extension de l'en-tête CSV ; reprise après plantage.
- `EndScreenDetector` : suites d'écrans simulés (rien, fin, fin, fin, rien…) → exactement un résultat ;
  lecture instable → résultat `unstable` après 3 s ; titre disparu avant stabilité → résultat `unstable`, émis une seule fois.
- `DigitReader` : apprentissage puis relecture sur des nombres synthétiques et sur les vraies captures.
- `EndScreenReader` : sur les captures plein écran réelles dans `tests/fixtures/` → bonne issue, bonnes ressources, bons montants.

Les tests sur captures réelles attendent les fixtures fournies par l'utilisateur (écran COMPLETED et écran FAILED
plein écran, non recadrés).

## 10. Hors périmètre

- Résolutions multiples / redimensionnement de Roblox.
- Détail mission par mission dans un fichier.
- Statistiques de la section STATS (temps, dégâts, titans tués).
- Toute action automatique déclenchée par le compteur.

## 11. Fichiers touchés

- Nouveau : `mission_stats.py`, `tests/test_mission_stats.py`, `tests/fixtures/*.png`, dossier `stats/`.
- Modifié : `macro_gui.py` (MissionWatcher, Hud, onglet Session, réglages HUD, cycle de vie de la session).
- Inchangés : `roblox_macro.py`, `build_exe.bat`, `triggers.json`, `scenarios.json`.
