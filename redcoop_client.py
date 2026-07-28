#!/usr/bin/env python3
import asyncio
import json
import os
import re
import sys

import websockets

# When packaged into a standalone .exe (PyInstaller), sys.executable is the
# .exe itself; as a plain script, __file__ is this .py file. Either way,
# BASE_DIR ends up being the folder this program lives in - the whole point
# is "drop this next to REDCoop.log in the mod folder and just double-click
# it", zero command-line arguments required for the common case. CLI
# arguments still override, for testing/advanced setups.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _default(argv_index, filename):
    return sys.argv[argv_index] if len(sys.argv) > argv_index else os.path.join(BASE_DIR, filename)


LOG_PATH = _default(1, "REDCoop.log")
FRIEND_POS_PATH = _default(3, "friend_pos.json")
OWN_OUTFIT_PATH = _default(4, "own_outfit.json")
FRIEND_OUTFIT_PATH = _default(5, "friend_outfit.json")
# Not exposed as CLI arguments (matches the "just drop it in the mod folder"
# zero-config philosophy) - init.lua always writes/reads these right next to
# REDCoop.log, in the MOD's folder - which is NOT necessarily BASE_DIR (the
# folder this script/exe lives in): when run as a plain script from
# ~/redcoop-scripts with an explicit REDCoop.log path passed in, the mod
# folder and BASE_DIR are two different places. Derive from LOG_PATH's own
# directory instead, so it's correct in both that setup AND the future
# zero-argument packaged .exe case (where LOG_PATH itself defaults to
# BASE_DIR, making the two the same anyway).
_MOD_DIR = os.path.dirname(LOG_PATH) or BASE_DIR
OWN_VEHICLE_PATH = os.path.join(_MOD_DIR, "own_vehicle.json")
FRIEND_VEHICLE_PATH = os.path.join(_MOD_DIR, "friend_vehicle.json")
OWN_FACE_PATH = os.path.join(_MOD_DIR, "own_face.json")
FRIEND_FACE_PATH = os.path.join(_MOD_DIR, "friend_face.json")
RELAY_URL = "ws://sakura-launcher.duckdns.org:8765"

LOG_FILE_PATH = os.path.join(BASE_DIR, "redcoop_client.log")


def log(msg):
    # Dual-logged (print + file): the packaged .exe is built with
    # PyInstaller --noconsole (no visible window at all), so a plain
    # print() would go nowhere anyone could ever see. redcoop_client.log
    # next to the exe is where to look if something's wrong.
    print(msg)
    try:
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def _read_config():
    config_path = os.path.join(os.path.dirname(LOG_PATH) or ".", "redcoop_config.json")
    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _cli_name():
    return sys.argv[2] if len(sys.argv) > 2 else None


def _cli_room():
    return sys.argv[6] if len(sys.argv) > 6 else None


POS_RE = re.compile(r"pos x=(-?\d+\.\d+) y=(-?\d+\.\d+) z=(-?\d+\.\d+)")


async def tail(path):
    # Create the file if missing instead of crashing - matters on a
    # brand-new install, before the mod has ever written to REDCoop.log yet.
    if not os.path.exists(path):
        open(path, "a").close()
    with open(path, "r") as f:
        f.seek(0, 2)  # start at end of file, only forward
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.2)
                continue
            yield line


def read_own_outfit():
    if not OWN_OUTFIT_PATH:
        return None
    try:
        with open(OWN_OUTFIT_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_own_vehicle():
    # init.lua writes "{}" (not an error) when not in a vehicle - treat an
    # empty dict the same as "no vehicle" (None), matching read_own_outfit's
    # nil-if-empty behavior.
    try:
        with open(OWN_VEHICLE_PATH, "r") as f:
            data = json.load(f)
            return data if data else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_own_face():
    # Only written when the player has used the "Capturer mon visage" hotkey
    # with the mirror open (see writeOwnFace in init.lua) - most of the time
    # this file won't exist yet, which is fine, same as outfit/vehicle.
    try:
        with open(OWN_FACE_PATH, "r") as f:
            data = json.load(f)
            return data if data else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json_atomic(path, data):
    # Bug found live tonight: a bare "<path>.tmp" collides if two
    # redcoop_client.py processes ever run at once (e.g. one left over from a
    # previous game session that didn't get killed, plus a fresh one) - both
    # write the same .tmp name, and whichever one calls os.replace() second
    # gets "No such file or directory" because the first one already
    # consumed it. Including the PID makes each process's tmp file unique,
    # so two processes writing the same target path can no longer collide.
    if not path:
        return
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.replace(tmp_path, path)  # atomic on the same filesystem


async def sender(ws):
    async for line in tail(LOG_PATH):
        match = POS_RE.search(line)
        if not match:
            continue
        x, y, z = (float(v) for v in match.groups())
        outfit = read_own_outfit()
        vehicle = read_own_vehicle()
        face = read_own_face()
        await ws.send(json.dumps(
            {"type": "pos", "x": x, "y": y, "z": z, "outfit": outfit, "vehicle": vehicle, "face": face}))
        face_desc = f"oui({face.get('count')})" if face else "non"
        log(f"[client] envoye: x={x} y={y} z={z} outfit={'oui' if outfit else 'non'} vehicule={'oui' if vehicle else 'non'} visage={face_desc}")


def _clear_friend_data():
    # Wipes all "last known friend state" files - init.lua's readFriendPos()
    # treats an empty {} the same as "no data" and despawns the puppet.
    # Used both when the relay tells us the friend left (msg type "leave")
    # and when WE lose our own connection to the relay (main()'s except
    # branch) - either way we no longer have live data to show.
    write_json_atomic(FRIEND_POS_PATH, {})
    write_json_atomic(FRIEND_OUTFIT_PATH, {})
    write_json_atomic(FRIEND_VEHICLE_PATH, {})
    write_json_atomic(FRIEND_FACE_PATH, {})


async def receiver(ws):
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "pos":
            log(f"[client] recu de {msg.get('from')}: x={msg['x']} y={msg['y']} z={msg['z']}")
            write_json_atomic(FRIEND_POS_PATH, {"from": msg.get("from"), "x": msg["x"], "y": msg["y"], "z": msg["z"]})
            if msg.get("outfit"):
                write_json_atomic(FRIEND_OUTFIT_PATH, msg["outfit"])
            # Always write (even "{}"): if the friend just got OUT of their
            # vehicle, we need that to arrive too, or init.lua would keep
            # thinking they're still driving forever (stale last-known state).
            write_json_atomic(FRIEND_VEHICLE_PATH, msg.get("vehicle") or {})
            face = msg.get("face")
            if face:
                log(f"[client] visage recu de {msg.get('from')}: count={face.get('count')}")
                write_json_atomic(FRIEND_FACE_PATH, face)
        elif msg.get("type") == "leave":
            # BUG FIXED tonight (live test with Enzo): previously nothing
            # ever told init.lua the friend was gone - their puppet stayed
            # in the game forever after they disconnected. The relay now
            # broadcasts this the moment someone leaves the room.
            log(f"[client] {msg.get('from')} s'est deconnecte, effacement de ses donnees")
            _clear_friend_data()


async def wait_for_room_and_name():
    # Polls redcoop_config.json (written by the in-game connect window)
    # until a room code is set, instead of exiting immediately - lets this
    # be started before the player has typed a room code in-game yet; it
    # picks the code up automatically as soon as they hit "Enregistrer",
    # no restart needed. Required: without a room code we refuse to guess
    # one, since guessing wrong means either total silence or, worse,
    # syncing with a random stranger also running the mod.
    printed_waiting = False
    while True:
        config = _read_config()
        name = _cli_name() or config.get("name") or "joueur"
        room = _cli_room() or config.get("room") or None
        if room:
            return name, room
        if not printed_waiting:
            log("[client] en attente d'un code de salle (ouvre la fenetre REDCoop en jeu et clique Enregistrer)...")
            printed_waiting = True
        await asyncio.sleep(3)


async def main():
    log(f"[client] demarrage, log surveille: {LOG_PATH}")
    name, room = await wait_for_room_and_name()
    log(f"[client] connexion a {RELAY_URL} sous le nom '{name}', salle '{room}'")
    log(f"[client] positions recues ecrites dans {FRIEND_POS_PATH}")
    log(f"[client] tenue lue depuis {OWN_OUTFIT_PATH}")
    log(f"[client] tenue de l'ami ecrite dans {FRIEND_OUTFIT_PATH}")

    while True:
        try:
            async with websockets.connect(RELAY_URL) as ws:
                await ws.send(json.dumps({"type": "hello", "name": name, "room": room}))
                log("[client] connecte au relais")
                await asyncio.gather(sender(ws), receiver(ws))
        except Exception as e:
            log(f"[client] connexion perdue ({e}), nouvelle tentative dans 3s...")
            _clear_friend_data()
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
