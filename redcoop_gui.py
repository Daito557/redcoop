
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
import asyncio
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import filedialog, messagebox, ttk

import websockets

RELAY_URL = "ws://sakura-launcher.duckdns.org:8765"
HTTP_BASE = "http://sakura-launcher.duckdns.org:8765"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "redcoop_gui_config.json")

POS_RE = re.compile(r"pos x=(-?\d+\.\d+) y=(-?\d+\.\d+) z=(-?\d+\.\d+)")

# ---- Palette "HUD cyberpunk" (assortie au logo/serveur Discord REDCoop) ----
BG = "#0b0b0d"
BG_PANEL = "#141417"
FG = "#e8e8ea"
FG_DIM = "#8a8a90"
RED = "#ff2a45"
RED_DIM = "#7a1420"
CYAN = "#37e0ff"
FONT_UI = ("DejaVu Sans Mono", 10)
FONT_TITLE = ("DejaVu Sans Mono", 20, "bold")
FONT_LABEL = ("DejaVu Sans Mono", 9, "bold")


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
    tmp = path + ".tmp"
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
    pilotable (start/stop) plutot qu'un script a arguments."""

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
        self._stop = False
        self._ws = None

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

    async def _sender(self, ws):
        async for line in self._tail():
            match = POS_RE.search(line)
            if not match:
                continue
            x, y, z = (float(v) for v in match.groups())
            outfit = read_json(self.own_outfit_path)
            vehicle = read_json(self.own_vehicle_path)
            await ws.send(json.dumps({"type": "pos", "x": x, "y": y, "z": z, "outfit": outfit, "vehicle": vehicle}))
            self.log(f"envoye: x={x} y={y} z={z}")

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
                write_json_atomic(self.friend_pos_path, {"from": msg.get("from"), "x": msg["x"], "y": msg["y"], "z": msg["z"]})
                if msg.get("outfit"):
                    write_json_atomic(self.friend_outfit_path, msg["outfit"])
                write_json_atomic(self.friend_vehicle_path, msg.get("vehicle") or {})
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
        self.log("arrete.")


def _style_widgets(root):
    """Theme sombre/neon 'HUD cyberpunk' assorti au logo REDCoop - applique
    globalement via ttk.Style. Le theme 'clam' est utilise comme base car,
    contrairement aux themes natifs (aqua/vista), il respecte vraiment les
    couleurs qu'on lui donne sur tous les OS."""
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=BG, foreground=FG, font=FONT_UI)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=BG_PANEL)
    style.configure("TLabel", background=BG, foreground=FG, font=FONT_UI)
    style.configure("Section.TLabel", background=BG, foreground=CYAN, font=FONT_LABEL)
    style.configure("Title.TLabel", background=BG, foreground=RED, font=FONT_TITLE)
    style.configure("Status.TLabel", background=BG, foreground=FG_DIM, font=FONT_UI)

    style.configure("TEntry", fieldbackground=BG_PANEL, foreground=FG,
                    insertcolor=FG, bordercolor=RED_DIM, lightcolor=BG_PANEL,
                    darkcolor=BG_PANEL, padding=4)
    style.map("TEntry", bordercolor=[("focus", RED)])

    style.configure("TButton", background=BG_PANEL, foreground=FG,
                    bordercolor=RED_DIM, lightcolor=BG_PANEL, darkcolor=BG_PANEL,
                    padding=(10, 6), font=FONT_UI)
    style.map("TButton",
              background=[("active", RED_DIM), ("disabled", BG)],
              foreground=[("disabled", FG_DIM)],
              bordercolor=[("active", RED)])

    style.configure("Accent.TButton", background=RED_DIM, foreground=FG,
                     bordercolor=RED, padding=(10, 6), font=("DejaVu Sans Mono", 10, "bold"))
    style.map("Accent.TButton",
              background=[("active", RED), ("disabled", BG)],
              foreground=[("disabled", FG_DIM)])


class App:
    def __init__(self, root):
        self.root = root
        root.title("REDCoop")
        root.geometry("620x560")
        root.resizable(False, False)
        _style_widgets(root)

        cfg = load_config()
        self.mod_dir_var = tk.StringVar(value=os.path.expanduser(cfg.get("mod_dir", "")))
        self.name_var = tk.StringVar(value=cfg.get("name", ""))
        self.room_var = tk.StringVar(value=cfg.get("room", ""))

        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 4))
        ttk.Label(header, text="REDCOOP", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=" // COOP CYBERPUNK 2077", style="Status.TLabel").pack(side="left", padx=(4, 0))

        ttk.Frame(outer, height=2, style="Panel.TFrame").pack(fill="x", pady=(4, 14))

        ttk.Label(outer, text="DOSSIER DU MOD (contient init.lua)", style="Section.TLabel").pack(anchor="w")
        row1 = ttk.Frame(outer)
        row1.pack(fill="x", pady=(3, 14))
        ttk.Entry(row1, textvariable=self.mod_dir_var, width=52).pack(side="left", fill="x", expand=True)
        ttk.Button(row1, text="Parcourir...", command=self.browse).pack(side="left", padx=(6, 0))

        two_col = ttk.Frame(outer)
        two_col.pack(fill="x", pady=(0, 14))
        col_a = ttk.Frame(two_col)
        col_a.pack(side="left", fill="x", expand=True)
        ttk.Label(col_a, text="TON NOM", style="Section.TLabel").pack(anchor="w")
        ttk.Entry(col_a, textvariable=self.name_var, width=26).pack(anchor="w", pady=(3, 0))
        col_b = ttk.Frame(two_col)
        col_b.pack(side="left", fill="x", expand=True, padx=(16, 0))
        ttk.Label(col_b, text="CODE DE SALLE", style="Section.TLabel").pack(anchor="w")
        ttk.Entry(col_b, textvariable=self.room_var, width=26).pack(anchor="w", pady=(3, 0))

        btn_row = ttk.Frame(outer)
        btn_row.pack(fill="x", pady=(2, 10))
        self.start_btn = ttk.Button(btn_row, text="DEMARRER", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btn_row, text="ARRETER", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.status_var = tk.StringVar(value="● ARRETE")
        ttk.Label(btn_row, textvariable=self.status_var, style="Status.TLabel").pack(side="left", padx=(16, 0))
        self.ping_var = tk.StringVar(value="ping: --")
        ttk.Label(btn_row, textvariable=self.ping_var, style="Status.TLabel").pack(side="left", padx=(16, 0))

        update_row = ttk.Frame(outer)
        update_row.pack(fill="x", pady=(0, 14))
        self.update_btn = ttk.Button(update_row, text="Installer / mettre a jour le mod",
                                      command=self.install_update)
        self.update_btn.pack(side="left")
        self.update_status_var = tk.StringVar(value="")
        ttk.Label(update_row, textvariable=self.update_status_var, style="Status.TLabel").pack(side="left", padx=(10, 0))

        ttk.Label(outer, text="JOURNAL", style="Section.TLabel").pack(anchor="w")
        log_frame = ttk.Frame(outer, style="Panel.TFrame")
        log_frame.pack(fill="both", expand=True, pady=(3, 0))
        self.log_text = tk.Text(log_frame, height=13, width=74, state="disabled",
                                 bg=BG_PANEL, fg=FG, insertbackground=FG,
                                 relief="flat", font=("DejaVu Sans Mono", 9),
                                 highlightthickness=1, highlightbackground=RED_DIM,
                                 highlightcolor=RED)
        self.log_text.pack(fill="both", expand=True, padx=1, pady=1)

        self.client = None
        self.thread = None
        self.loop = None
        self._lock_path = None
        self.log_queue = queue.Queue()
        self.ping_queue = queue.Queue()
        root.after(150, self._drain_log_queue)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def browse(self):
        d = filedialog.askdirectory(title="Choisis le dossier mods/REDCoop (contient init.lua)")
        if d:
            self.mod_dir_var.set(d)

    def log(self, msg):
        # Appele depuis le thread reseau - jamais toucher directement un
        # widget Tkinter depuis un autre thread, on passe par la queue et
        # _drain_log_queue() (qui tourne dans le thread principal) l'affiche.
        self.log_queue.put(msg)

    def _on_ping(self, rtt_ms):
        # Meme regle que log() ci-dessus : appele depuis le thread reseau,
        # on passe par une queue plutot que de toucher ping_var directement.
        self.ping_queue.put(rtt_ms)

    def _drain_log_queue(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        while True:
            try:
                rtt_ms = self.ping_queue.get_nowait()
            except queue.Empty:
                break
            self.ping_var.set(f"ping: {rtt_ms} ms" if rtt_ms is not None else "ping: --")
        self.root.after(150, self._drain_log_queue)

    def install_update(self):
        # Telecharge init.lua depuis le relais (endpoint HTTP /files/init.lua
        # ajoute a index.js) et l'installe directement dans le dossier
        # choisi - le "on heberge le nouveau fichier sur le host et ca
        # envoie au client" demande ce soir.
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
        mod_dir = os.path.expanduser(self.mod_dir_var.get().strip())
        name = self.name_var.get().strip()
        room = self.room_var.get().strip()

        if not mod_dir or not os.path.isdir(mod_dir):
            messagebox.showerror("REDCoop", "Choisis d'abord un dossier de mod valide (contient init.lua).")
            return
        if not name or not room:
            messagebox.showerror("REDCoop", "Le nom et le code de salle sont obligatoires.")
            return

        # Garde-fou "un seul a la fois" - le bug trouve en live ce soir
        # (deux redcoop_client lances par erreur sur deux salles differentes,
        # sans que personne ne s'en rende compte) ne peut plus arriver sans
        # au moins un avertissement explicite.
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

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("● CONNECTE / EN COURS")

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
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("● ARRETE")
        self.ping_var.set("ping: --")

    def on_close(self):
        self.stop()
        self.root.after(300, self.root.destroy)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
