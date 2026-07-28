#!/usr/bin/env python3
# REDCoop - application graphique (remplace redcoop_client.py + la ligne de
# commande). Plus aucun argument a taper/mal orthographier - le dossier du
# mod, le nom et le code de salle se choisissent une fois dans la fenetre et
# sont memorises pour la prochaine fois (redcoop_gui_config.json, a cote de
# l'executable). Corrige aussi la classe de bugs vue en live ce soir :
# arguments PowerShell decales (nom = chemin de fichier par erreur) et
# plusieurs clients lances par erreur sur des salles differentes sans que
# personne ne s'en rende compte (garde-fou "un seul a la fois" ci-dessous).
#
# Le bouton "Installer / mettre a jour le mod" telecharge init.lua depuis
# le relais (endpoint HTTP /files/... ajoute a index.js) et l'installe
# directement dans le dossier choisi - plus besoin de copier le fichier a
# la main a chaque nouvelle version.
#
# INTERFACE "NETLINK" (ce soir) - refonte visuelle demandee pour ressembler a
# une maquette HUD cyberpunk (panneaux neon, hexagone central, boutons en
# pilule). Reste en Tkinter/Canvas (pas de nouvelle dependance -> le pipeline
# .exe existant continue de marcher tel quel) plutot qu'une vraie page HTML :
# Tkinter n'a pas de vrai flou/glow ni de degrades natifs, donc le rendu est
# une approximation (contours neon empiles, pas un vrai bloom) - mais garde
# la mise en page (panneaux YOU / LINK <ami>, hexagone ONLINE + ping,
# barre de statut) fidele a la maquette, avec de vraies donnees derriere
# chaque valeur affichee plutot que du texte fixe.
import asyncio
import json
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import filedialog, messagebox

import websockets

RELAY_URL = "ws://sakura-launcher.duckdns.org:8765"
HTTP_BASE = "http://sakura-launcher.duckdns.org:8765"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "redcoop_gui_config.json")

POS_RE = re.compile(r"pos x=(-?\d+\.\d+) y=(-?\d+\.\d+) z=(-?\d+\.\d+)")

# ---- Palette "HUD cyberpunk" (assortie a la maquette NETLINK) ----
BG = "#07070a"
PANEL_BG = "#120a10"
PANEL_BORDER = "#ff2a58"
PANEL_BORDER_DIM = "#5c1428"
CYAN = "#39f2d8"
CYAN_DIM = "#0f5c52"
FG = "#f0eef2"
FG_DIM = "#8a8290"
GREEN = "#3dffa0"
RED = "#ff3b5c"
FONT_TITLE = ("DejaVu Sans Mono", 22, "bold")
FONT_SUB = ("DejaVu Sans Mono", 9)
FONT_PANEL_TITLE = ("DejaVu Sans Mono", 13, "bold")
FONT_ROW = ("DejaVu Sans Mono", 11)
FONT_BIG = ("DejaVu Sans Mono", 15, "bold")
FONT_HUGE = ("DejaVu Sans Mono", 19, "bold")
FONT_STATUSBAR = ("DejaVu Sans Mono", 10, "bold")


def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
    except OSError:
        pass


def write_json_atomic(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class RedcoopClient:
    """Meme logique reseau que redcoop_client.py (tail du log, envoi/reception
    des positions, nettoyage a la deconnexion), packagee comme une classe
    pilotable (start/stop) plutot qu'un script a arguments. Etend l'etat
    partage (self.state, verrouille par self.state_lock) avec tout ce dont
    l'interface NETLINK a besoin pour s'afficher - c'est l'UI (App, thread
    principal) qui lit cet etat a intervalle regulier, jamais l'inverse."""

    def __init__(self, mod_dir, name, room, log_fn, ping_fn=None):
        self.mod_dir = mod_dir
        self.name = name
        self.room = room
        self.log = log_fn
        self.ping_fn = ping_fn or (lambda rtt_ms: None)
        self.log_path = os.path.join(mod_dir, "REDCoop.log")
        self.friend_pos_path = os.path.join(mod_dir, "friend_pos.json")
        self.own_outfit_path = os.path.join(mod_dir, "own_outfit.json")
        self.friend_outfit_path = os.path.join(mod_dir, "friend_outfit.json")
        self.own_vehicle_path = os.path.join(mod_dir, "own_vehicle.json")
        self.friend_vehicle_path = os.path.join(mod_dir, "friend_vehicle.json")
        self.own_ping_path = os.path.join(mod_dir, "own_ping.json")
        self.own_face_path = os.path.join(mod_dir, "own_face.json")
        self.friend_face_path = os.path.join(mod_dir, "friend_face.json")
        self._stop = False
        self._ws = None

        self.state_lock = threading.Lock()
        self.state = {
            "own_pos": None,       # {x,y,z}
            "friend_pos": None,    # {x,y,z,from}
            "own_vehicle": None,
            "friend_vehicle": None,
            "own_face_count": None,
            "friend_face_count": None,
            "friend_seat": None,   # "driver" / "passenger" / None
            "connected": False,
        }

    def _set_state(self, **kwargs):
        with self.state_lock:
            self.state.update(kwargs)

    def get_state(self):
        with self.state_lock:
            return dict(self.state)

    def request_stop(self):
        self._stop = True

    async def _close_ws(self):
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    def _clear_friend_data(self):
        write_json_atomic(self.friend_pos_path, {})
        write_json_atomic(self.friend_outfit_path, {})
        write_json_atomic(self.friend_vehicle_path, {})
        self._set_state(friend_pos=None, friend_vehicle=None, connected=False)

    async def _tail(self):
        if not os.path.exists(self.log_path):
            open(self.log_path, "a").close()
        with open(self.log_path, "r") as f:
            f.seek(0, 2)
            while not self._stop:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.2)
                    continue
                yield line

    def _read_own_face_count(self):
        face = read_json(self.own_face_path)
        if isinstance(face, dict) and "count" in face:
            return face["count"]
        return None

    def _read_friend_face_count(self):
        face = read_json(self.friend_face_path)
        if isinstance(face, dict) and "count" in face:
            return face["count"]
        return None

    async def _sender(self, ws):
        async for line in self._tail():
            match = POS_RE.search(line)
            if not match:
                continue
            x, y, z = (float(v) for v in match.groups())
            outfit = read_json(self.own_outfit_path)
            vehicle = read_json(self.own_vehicle_path)
            face = read_json(self.own_face_path)
            await ws.send(json.dumps(
                {"type": "pos", "x": x, "y": y, "z": z, "outfit": outfit, "vehicle": vehicle, "face": face}))
            self.log(f"envoye: x={x} y={y} z={z}")
            self._set_state(
                own_pos={"x": x, "y": y, "z": z},
                own_vehicle=vehicle,
                own_face_count=self._read_own_face_count(),
            )

    async def _pinger(self, ws):
        # Mesure la latence vers le relais - un simple aller-retour toutes
        # les 5s, jamais diffuse a l'autre joueur (voir index.js: le "pong"
        # est renvoye uniquement a l'expediteur).
        while not self._stop:
            try:
                await ws.send(json.dumps({"type": "ping", "ts": time.time()}))
            except Exception:
                return
            await asyncio.sleep(5)

    async def _receiver(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "pos":
                self.log(f"recu de {msg.get('from')}: x={msg['x']} y={msg['y']} z={msg['z']}")
                write_json_atomic(self.friend_pos_path,
                                   {"from": msg.get("from"), "x": msg["x"], "y": msg["y"], "z": msg["z"]})
                if msg.get("outfit"):
                    write_json_atomic(self.friend_outfit_path, msg["outfit"])
                write_json_atomic(self.friend_vehicle_path, msg.get("vehicle") or {})
                face = msg.get("face")
                if face:
                    write_json_atomic(self.friend_face_path, face)
                self._set_state(
                    friend_pos={"x": msg["x"], "y": msg["y"], "z": msg["z"], "from": msg.get("from")},
                    friend_vehicle=(msg.get("vehicle") or None),
                    friend_face_count=(face.get("count") if isinstance(face, dict) else None),
                    connected=True,
                )
            elif msg.get("type") == "leave":
                self.log(f"{msg.get('from')} s'est deconnecte, effacement de ses donnees")
                self._clear_friend_data()
            elif msg.get("type") == "pong":
                try:
                    rtt_ms = int((time.time() - float(msg["ts"])) * 1000)
                except (TypeError, KeyError, ValueError):
                    continue
                self.ping_fn(rtt_ms)
                # Own ping to the relay, written for init.lua to read - a
                # reasonable proxy for how stale THIS client's incoming
                # friend-position samples might be, used to widen the
                # vehicle extrapolation window when the connection is laggy.
                write_json_atomic(self.own_ping_path, {"rtt_ms": rtt_ms})

    async def run(self):
        self.log(f"demarrage - dossier: {self.mod_dir}")
        while not self._stop:
            try:
                async with websockets.connect(RELAY_URL) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"type": "hello", "name": self.name, "room": self.room}))
                    self.log(f"connecte au relais sous '{self.name}', salle '{self.room}'")
                    self._set_state(connected=True)
                    await asyncio.gather(self._sender(ws), self._receiver(ws), self._pinger(ws))
            except Exception as e:
                if self._stop:
                    break
                self.log(f"connexion perdue ({e}), nouvelle tentative dans 3s...")
                self._clear_friend_data()
                self.ping_fn(None)
                await asyncio.sleep(3)
            finally:
                self._ws = None
        self._set_state(connected=False)
        self.log("arrete.")


def _rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    """Tkinter Canvas n'a pas de primitive 'rectangle arrondi' - construite a
    la main a partir d'un polygone avec des coins lisses (smooth=True)."""
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def _neon_rect(canvas, x1, y1, x2, y2, radius, color, bg, layers=3):
    """Simule un halo neon en Tkinter (qui n'a pas de vrai flou/glow) en
    empilant plusieurs contours du meme rectangle arrondi, du plus large/plus
    sombre vers le plus fin/plus clair - pas un vrai bloom, mais donne une
    impression de lueur a distance normale."""
    for i in range(layers, 0, -1):
        expand = i * 2
        _rounded_rect(canvas, x1 - expand, y1 - expand, x2 + expand, y2 + expand, radius + expand,
                       outline=color, width=1, fill="")
    _rounded_rect(canvas, x1, y1, x2, y2, radius, outline=color, width=2, fill=bg)


def _pill_button(canvas, x1, y1, x2, y2, text, fg, border, fill, font, command):
    radius = (y2 - y1) / 2
    shape = _rounded_rect(canvas, x1, y1, x2, y2, radius, outline=border, width=2, fill=fill)
    label = canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=text, fill=fg, font=font)

    def _on_click(_event):
        command()

    for item in (shape, label):
        canvas.tag_bind(item, "<Button-1>", _on_click)
    return shape, label


class App:
    def __init__(self, root):
        self.root = root
        root.title("REDCoop // NETLINK")
        root.geometry("980x680")
        root.configure(bg=BG)
        root.resizable(False, False)

        cfg = load_config()
        self.mod_dir_var = tk.StringVar(value=os.path.expanduser(cfg.get("mod_dir", "")))
        self.name_var = tk.StringVar(value=cfg.get("name", ""))
        self.room_var = tk.StringVar(value=cfg.get("room", ""))

        self.client = None
        self.thread = None
        self.loop = None
        self._lock_path = None
        self.log_lines = []
        self.log_queue = queue.Queue()
        self.ping_queue = queue.Queue()
        self.current_ping_ms = None
        self.running = False

        self.canvas = tk.Canvas(root, width=980, height=680, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self._build_static_layout()
        self._build_config_row()
        self._build_dynamic_placeholders()

        root.after(150, self._drain_queues)
        root.after(200, self._refresh_dynamic)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------------- UI --

    def _build_static_layout(self):
        c = self.canvas
        # Fond "espace" leger: quelques points fixes, discrets - pas une vraie
        # texture d'etoiles animee, juste assez pour casser le noir uni.
        for seed in range(70):
            x = (seed * 137) % 980
            y = (seed * 89) % 680
            size = 1 if seed % 3 else 2
            shade = "#1a1420" if seed % 2 else "#120e18"
            c.create_oval(x, y, x + size, y + size, fill=shade, outline="")

        c.create_text(490, 60, text="REDCOOP", fill=PANEL_BORDER, font=FONT_TITLE)
        c.create_text(490, 90, text="N E T L I N K", fill=CYAN, font=("DejaVu Sans Mono", 12, "bold"))

        # ---- Panneau gauche: YOU ----
        _neon_rect(c, 40, 140, 400, 470, 16, PANEL_BORDER, PANEL_BG)
        c.create_text(70, 168, text="YOU", fill=FG, font=FONT_PANEL_TITLE, anchor="w")

        # ---- Panneau droit: LINK <ami> ----
        _neon_rect(c, 580, 140, 940, 470, 16, PANEL_BORDER, PANEL_BG)
        self.link_title_id = c.create_text(610, 168, text="LINK ---", fill=FG, font=FONT_PANEL_TITLE, anchor="w")

        # ---- Hexagone central (statut connexion) ----
        self.hex_id = self._draw_hexagon(490, 300, 70, CYAN_DIM, CYAN)
        # Glyphe simple dessine a la main (pas un caractere Unicode/emoji -
        # rendu peu fiable selon les polices installees, confirme en testant
        # le rendu reel: "⛓" s'affichait comme "88" illisible) - deux petits
        # cercles relies, evoque un lien/connexion sans dependre d'une police.
        self.hex_glyph_oval1 = c.create_oval(472, 292, 486, 306, outline=CYAN, width=2)
        self.hex_glyph_oval2 = c.create_oval(494, 292, 508, 306, outline=CYAN, width=2)
        self.hex_glyph_line = c.create_line(486, 299, 494, 299, fill=CYAN, width=2)
        self.online_text_id = c.create_text(490, 400, text="OFFLINE", fill=FG_DIM, font=FONT_HUGE)
        self.ping_text_id = c.create_text(490, 428, text="--", fill=FG_DIM, font=FONT_ROW)

        # ---- Boutons pilule ----
        _pill_button(c, 330, 560, 500, 610, "DEMARRER", "#052b18", GREEN, GREEN,
                     ("DejaVu Sans Mono", 13, "bold"), self.start)
        _pill_button(c, 520, 560, 690, 610, "STOPPER", "#2b0510", RED, RED,
                     ("DejaVu Sans Mono", 13, "bold"), self.stop)

        # ---- Barre de statut (bas) ----
        self.statusbar_id = c.create_text(
            490, 645, text="NET -- | SYNC -- | VEH -- | FACE --",
            fill=CYAN, font=FONT_STATUSBAR)

    def _draw_hexagon(self, cx, cy, r, fill, outline):
        points = []
        for i in range(6):
            angle = math.pi / 3 * i - math.pi / 2
            points.append(cx + r * math.cos(angle))
            points.append(cy + r * math.sin(angle))
        return self.canvas.create_polygon(points, fill=fill, outline=outline, width=2)

    def _build_config_row(self):
        # Les vrais reglages (dossier du mod / nom / code de salle) restent
        # de simples widgets Tkinter poses PAR-DESSUS le canvas plutot que
        # dessines a la main - ce sont des vrais champs de saisie, pas des
        # decorations, donc mieux vaut garder les vrais widgets natifs
        # (focus clavier, copier-coller, etc. gratuits).
        f = tk.Frame(self.root, bg=PANEL_BG, highlightbackground=PANEL_BORDER_DIM, highlightthickness=1)
        f.place(x=40, y=480, width=900, height=90)

        tk.Label(f, text="DOSSIER DU MOD", bg=PANEL_BG, fg=CYAN, font=("DejaVu Sans Mono", 8, "bold")) \
            .place(x=10, y=6)
        tk.Entry(f, textvariable=self.mod_dir_var, bg="#1a1218", fg=FG, insertbackground=FG,
                  relief="flat", font=("DejaVu Sans Mono", 9), width=52).place(x=10, y=26)
        tk.Button(f, text="Parcourir...", command=self.browse, bg="#1a1218", fg=FG,
                  activebackground=PANEL_BORDER_DIM, relief="flat", font=("DejaVu Sans Mono", 8)) \
            .place(x=420, y=24)

        tk.Label(f, text="TON NOM", bg=PANEL_BG, fg=CYAN, font=("DejaVu Sans Mono", 8, "bold")).place(x=540, y=6)
        tk.Entry(f, textvariable=self.name_var, bg="#1a1218", fg=FG, insertbackground=FG,
                  relief="flat", font=("DejaVu Sans Mono", 9), width=14).place(x=540, y=26)

        tk.Label(f, text="CODE DE SALLE", bg=PANEL_BG, fg=CYAN, font=("DejaVu Sans Mono", 8, "bold")) \
            .place(x=700, y=6)
        tk.Entry(f, textvariable=self.room_var, bg="#1a1218", fg=FG, insertbackground=FG,
                  relief="flat", font=("DejaVu Sans Mono", 9), width=14).place(x=700, y=26)

        tk.Button(f, text="Installer / MAJ le mod", command=self.install_update, bg="#1a1218", fg=FG,
                  activebackground=PANEL_BORDER_DIM, relief="flat", font=("DejaVu Sans Mono", 8)) \
            .place(x=10, y=58)
        self.update_status_var = tk.StringVar(value="")
        tk.Label(f, textvariable=self.update_status_var, bg=PANEL_BG, fg=FG_DIM,
                 font=("DejaVu Sans Mono", 8)).place(x=170, y=60)

        # Journal, replie sous le canvas - garde le texte technique complet
        # disponible (utile pour le support/debug) sans polluer le look HUD.
        log_frame = tk.Frame(self.root, bg=PANEL_BG, highlightbackground=PANEL_BORDER_DIM, highlightthickness=1)
        log_frame.place(x=40, y=480, width=900, height=90)
        log_frame.place_forget()  # cache par defaut, bascule possible plus tard si besoin
        self._log_frame = log_frame

    def _build_dynamic_placeholders(self):
        c = self.canvas
        # ---- Lignes du panneau YOU ----
        self.you_rows = {}
        labels = [("pos", "Position"), ("veh", "Vehicule"), ("outfit", "Tenue"), ("face", "Visage")]
        y = 210
        for key, label in labels:
            c.create_text(70, y, text=label, fill=FG_DIM, font=FONT_SUB, anchor="w")
            value_id = c.create_text(370, y, text="--", fill=FG, font=FONT_ROW, anchor="e")
            self.you_rows[key] = value_id
            y += 62

        # ---- Panneau LINK <ami> ----
        # Barre de "signal" (juste decorative, refletee par connected=True/False)
        self.link_bar_bg = _rounded_rect(c, 610, 200, 910, 214, 7, outline=PANEL_BORDER_DIM, fill="#1a0a12")
        self.link_bar_fill = _rounded_rect(c, 610, 200, 610, 214, 7, outline="", fill=PANEL_BORDER)
        self.link_dist_left_id = c.create_text(610, 226, text="-- m", fill=FG_DIM, font=FONT_SUB, anchor="w")
        self.link_dist_right_id = c.create_text(910, 226, text="-- m", fill=FG_DIM, font=FONT_SUB, anchor="e")

        self.link_rows = {}
        link_labels = [("car", "Voiture partagee"), ("seat", "Siege"), ("face", "Visage")]
        y = 270
        for key, label in link_labels:
            c.create_text(610, y, text=label, fill=FG_DIM, font=FONT_SUB, anchor="w")
            value_id = c.create_text(910, y, text="--", fill=FG, font=FONT_ROW, anchor="e")
            self.link_rows[key] = value_id
            y += 40

        self.link_status_var_id = c.create_text(610, 420, text="hors ligne", fill=FG_DIM, font=FONT_ROW, anchor="w")

    # ------------------------------------------------------------ actions --

    def browse(self):
        d = filedialog.askdirectory(title="Choisis le dossier mods/REDCoop (contient init.lua)")
        if d:
            self.mod_dir_var.set(d)

    def log(self, msg):
        # Appele depuis le thread reseau - jamais toucher directement un
        # widget Tkinter depuis un autre thread, on passe par la queue.
        self.log_queue.put(msg)

    def _on_ping(self, rtt_ms):
        self.ping_queue.put(rtt_ms)

    def install_update(self):
        mod_dir = os.path.expanduser(self.mod_dir_var.get().strip())
        if not mod_dir:
            messagebox.showerror("REDCoop", "Choisis d'abord un dossier de mod (bouton Parcourir).")
            return
        os.makedirs(mod_dir, exist_ok=True)

        self.update_status_var.set("telechargement...")
        self.root.update_idletasks()
        try:
            url = f"{HTTP_BASE}/files/init.lua"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            self.update_status_var.set("echec")
            messagebox.showerror("REDCoop", f"Impossible de recuperer init.lua depuis le serveur :\n{e}")
            return

        dest = os.path.join(mod_dir, "init.lua")
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        self.update_status_var.set(f"installe ({len(data)} octets)")
        self.log(f"init.lua installe/mis a jour depuis le serveur ({len(data)} octets)")

    def start(self):
        if self.running:
            return
        mod_dir = os.path.expanduser(self.mod_dir_var.get().strip())
        name = self.name_var.get().strip()
        room = self.room_var.get().strip()

        if not mod_dir or not os.path.isdir(mod_dir):
            messagebox.showerror("REDCoop", "Choisis d'abord un dossier de mod valide (contient init.lua).")
            return
        if not name or not room:
            messagebox.showerror("REDCoop", "Le nom et le code de salle sont obligatoires.")
            return

        # Garde-fou "un seul a la fois" - deux redcoop_client lances par
        # erreur sur deux salles differentes, sans que personne ne s'en
        # rende compte, deja rencontre en live.
        lock_path = os.path.join(mod_dir, "redcoop_client.lock")
        if os.path.exists(lock_path):
            if not messagebox.askyesno(
                "REDCoop",
                "Un autre client REDCoop semble deja tourner pour ce dossier "
                "(fichier de verrou present).\n\nContinuer quand meme ?\n"
                "(reponds Non si tu as un doute - ferme d'abord l'autre fenetre)",
            ):
                return
        try:
            with open(lock_path, "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
        self._lock_path = lock_path

        save_config({"mod_dir": mod_dir, "name": name, "room": room})

        self.client = RedcoopClient(mod_dir, name, room, self.log, ping_fn=self._on_ping)
        self.thread = threading.Thread(target=self._run_client_thread, daemon=True)
        self.thread.start()
        self.running = True
        self.canvas.itemconfigure(self.link_title_id, text=f"LINK {name and room and self.room_var.get() or '---'}")

    def _run_client_thread(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.client.run())
        finally:
            self.loop.close()

    def stop(self):
        if self.client:
            self.client.request_stop()
            if self.loop is not None:
                try:
                    asyncio.run_coroutine_threadsafe(self.client._close_ws(), self.loop)
                except RuntimeError:
                    pass
        if self._lock_path and os.path.exists(self._lock_path):
            try:
                os.remove(self._lock_path)
            except OSError:
                pass
        self.running = False
        self.current_ping_ms = None

    def on_close(self):
        self.stop()
        self.root.after(300, self.root.destroy)

    # ---------------------------------------------------------- refresh --

    def _drain_queues(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_lines.append(msg)
            if len(self.log_lines) > 500:
                self.log_lines = self.log_lines[-500:]
        while True:
            try:
                rtt_ms = self.ping_queue.get_nowait()
            except queue.Empty:
                break
            self.current_ping_ms = rtt_ms
        self.root.after(150, self._drain_queues)

    def _refresh_dynamic(self):
        c = self.canvas
        state = self.client.get_state() if self.client else {}
        connected = bool(state.get("connected"))

        # ---- Hexagone / statut central ----
        glyph_color = CYAN if connected else FG_DIM
        if connected:
            c.itemconfigure(self.hex_id, fill=CYAN_DIM, outline=CYAN)
            c.itemconfigure(self.online_text_id, text="ONLINE", fill=GREEN)
        else:
            c.itemconfigure(self.hex_id, fill="#1a1218", outline=PANEL_BORDER_DIM)
            c.itemconfigure(self.online_text_id, text="OFFLINE", fill=FG_DIM)
        c.itemconfigure(self.hex_glyph_oval1, outline=glyph_color)
        c.itemconfigure(self.hex_glyph_oval2, outline=glyph_color)
        c.itemconfigure(self.hex_glyph_line, fill=glyph_color)

        ping_text = f"{self.current_ping_ms} ms" if self.current_ping_ms is not None else "--"
        c.itemconfigure(self.ping_text_id, text=ping_text, fill=(GREEN if connected else FG_DIM))

        # ---- Panneau YOU ----
        own_pos = state.get("own_pos")
        pos_text = f"{own_pos['x']:.0f}, {own_pos['y']:.0f}" if own_pos else "--"
        c.itemconfigure(self.you_rows["pos"], text=pos_text)

        own_vehicle = state.get("own_vehicle")
        c.itemconfigure(self.you_rows["veh"], text=("actif" if own_vehicle else "a pied"))

        outfit_path = self.client.own_outfit_path if self.client else None
        has_outfit = bool(read_json(outfit_path)) if outfit_path else False
        c.itemconfigure(self.you_rows["outfit"], text=("synced" if has_outfit else "--"))

        own_face_count = state.get("own_face_count")
        c.itemconfigure(self.you_rows["face"], text=(str(own_face_count) if own_face_count else "--"))

        # ---- Panneau LINK <ami> ----
        friend_pos = state.get("friend_pos")
        distance_m = None
        if own_pos and friend_pos:
            dx = friend_pos["x"] - own_pos["x"]
            dy = friend_pos["y"] - own_pos["y"]
            distance_m = math.sqrt(dx * dx + dy * dy)

        bar_ratio = 0.0
        if distance_m is not None:
            bar_ratio = max(0.0, min(1.0, 1.0 - (distance_m / 500.0)))
        bar_x2 = 610 + 300 * bar_ratio
        c.coords_updated = True
        try:
            c.delete(self.link_bar_fill)
        except Exception:
            pass
        self.link_bar_fill = _rounded_rect(c, 610, 200, max(610, bar_x2), 214, 7, outline="",
                                            fill=(CYAN if connected else PANEL_BORDER_DIM))
        c.tag_lower(self.link_bar_fill, self.link_dist_left_id)

        dist_text = f"{distance_m:.1f}m" if distance_m is not None else "-- m"
        c.itemconfigure(self.link_dist_left_id, text=dist_text)
        c.itemconfigure(self.link_dist_right_id, text=dist_text)

        friend_vehicle = state.get("friend_vehicle")
        c.itemconfigure(self.link_rows["car"], text=("oui" if friend_vehicle else "non"))
        c.itemconfigure(self.link_rows["seat"], text=(state.get("friend_seat") or "--"))
        friend_face_count = state.get("friend_face_count")
        c.itemconfigure(self.link_rows["face"], text=(str(friend_face_count) if friend_face_count else "--"))

        friend_name = friend_pos.get("from") if friend_pos else None
        title = f"LINK {friend_name or self.name_var.get() or '---'}"
        c.itemconfigure(self.link_title_id, text=title)
        c.itemconfigure(self.link_status_var_id,
                         text=("en ligne" if connected else "hors ligne"),
                         fill=(GREEN if connected else FG_DIM))

        # ---- Barre de statut ----
        net_txt = f"{ping_text}" if connected else "--"
        sync_txt = "STABLE" if connected else "OFFLINE"
        veh_txt = "OCCUPE" if own_vehicle else "LIBRE"
        face_txt = "PRET" if own_face_count else "--"
        c.itemconfigure(self.statusbar_id,
                         text=f"NET {net_txt} | SYNC {sync_txt} | VEH {veh_txt} | FACE {face_txt}",
                         fill=(CYAN if connected else FG_DIM))

        self.root.after(500, self._refresh_dynamic)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
