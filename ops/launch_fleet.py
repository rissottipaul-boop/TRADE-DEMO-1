"""Скрипт запуска демо-флота нативных ботов OKX (FLEET-DEMO).

Создаёт спот-грид и спот-DCA ботов с метками владельца trd (ORDER-OWNER-TAG),
соблюдая лимиты риска (<=5% equity на бота, обязательный SL).
"""

import subprocess
import time
import sys
from src.order_owner import new_client_order_id

GRID_BOTS = [
    {
        "inst": "SOL-USDT",
        "minPx": "112.21",
        "maxPx": "126.52",
        "gridNum": "25",
        "quoteSz": "1500",
        "sl": "108.84",
    },
    {
        "inst": "SUI-USDT",
        "minPx": "1.0678",
        "maxPx": "1.2406",
        "gridNum": "25",
        "quoteSz": "1200",
        "sl": "1.0358",
    },
    {
        "inst": "ADA-USDT",
        "minPx": "0.2283",
        "maxPx": "0.2626",
        "gridNum": "26",
        "quoteSz": "1100",
        "sl": "0.2214",
    },
    {
        "inst": "TRX-USDT",
        "minPx": "0.32496",
        "maxPx": "0.34506",
        "gridNum": "17",
        "quoteSz": "800",
        "sl": "0.31521",
    },
    {
        "inst": "ETC-USDT",
        "minPx": "8.48",
        "maxPx": "9.754",
        "gridNum": "26",
        "quoteSz": "800",
        "sl": "8.226",
    },
    {
        "inst": "APT-USDT",
        "minPx": "0.737",
        "maxPx": "0.856",
        "gridNum": "25",
        "quoteSz": "800",
        "sl": "0.715",
    },
]

DCA_BOTS = [
    {
        "inst": "XLM-USDT",
        "init": "60",
        "safety": "95",
        "maxSafety": "5",
        "pxSteps": "0.018",
        "pxStepsMult": "1.25",
        "volMult": "1.1",
        "tp": "0.018",
        "sl": "0.19",
    },
    {
        "inst": "DOT-USDT",
        "init": "60",
        "safety": "95",
        "maxSafety": "5",
        "pxSteps": "0.019",
        "pxStepsMult": "1.25",
        "volMult": "1.1",
        "tp": "0.019",
        "sl": "0.19",
    },
]

def run_okx_cli(args):
    cmd = ["okx", "--demo"] + args
    print(f"Running: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True, check=False, shell=True)
    out = (res.stdout or "") + (res.stderr or "")
    print(out.strip())
    if res.returncode != 0:
        raise RuntimeError(f"Command failed with code {res.returncode}: {out}")
    return out

def main():
    print("=== Launching remaining fleet bots ===")
    
    # 1. Grid bots
    for b in GRID_BOTS:
        cid = new_client_order_id("trd")
        args = [
            "bot", "grid", "create",
            "--instId", b["inst"],
            "--algoOrdType", "grid",
            "--maxPx", b["maxPx"],
            "--minPx", b["minPx"],
            "--gridNum", b["gridNum"],
            "--runType", "2",
            "--quoteSz", b["quoteSz"],
            "--slTriggerPx", b["sl"],
            "--algoClOrdId", cid,
        ]
        run_okx_cli(args)
        time.sleep(1.0)
        
    # 2. DCA bots
    for b in DCA_BOTS:
        cid = new_client_order_id("trd")
        args = [
            "bot", "dca", "create",
            "--algoOrdType", "spot_dca",
            "--instId", b["inst"],
            "--direction", "long",
            "--initOrdAmt", b["init"],
            "--safetyOrdAmt", b["safety"],
            "--maxSafetyOrds", b["maxSafety"],
            "--pxSteps", b["pxSteps"],
            "--pxStepsMult", b["pxStepsMult"],
            "--volMult", b["volMult"],
            "--tpPct", b["tp"],
            "--slPct", b["sl"],
            "--allowReinvest", "false",
            "--triggerStrategy", "instant",
            "--algoClOrdId", cid,
        ]
        run_okx_cli(args)
        time.sleep(1.0)

    print("=== All remaining bots created successfully! ===")

if __name__ == "__main__":
    main()
