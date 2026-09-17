# diag.py — full wire dump for one device_id
import os, sys, time, logging

os.environ["MLBB_COLOR"] = "0"       # plain text, no ANSI in log file
os.environ["MLBB_DEBUG_WIRE"] = "1"  # log every frame in/out

import checker_core as cc

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)

if len(sys.argv) < 2:
    print("usage: python diag.py and_<device_id>"); sys.exit(1)

did = sys.argv[1].strip()
print("=" * 70)
print("device_id :", did)
print("length    :", len(did))
print("version   :", cc.CLIENT_VERSION)
print("channel   :", cc.CHANNEL_AND if did.lower().startswith("and_") else cc.CHANNEL_IOS)
print("=" * 70)

t0 = time.time()
res = cc.check_device_id(did)
print("=" * 70)
print("elapsed   :", round(time.time() - t0, 2), "s")
print("status    :", res.get("status"))
if res.get("status") == "success":
    p = res["player_data"]
    for k in ("nickname", "player_id", "level", "current_rank", "skin_count"):
        print(f"{k:10s}:", p.get(k))
else:
    print("error     :", res.get("error"))
print("=" * 70)
