#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当前余额写进账本（cron 每 10 分钟一次），用于成本曲线。"""
import json, sys, time, datetime as dt
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from compshare.cs import CS, CompShareError

LEDGER = ROOT / ".compshare" / "ledger.jsonl"
LEDGER.parent.mkdir(parents=True, exist_ok=True)
try:
    bal = float(CS().balance().get("AmountAvailable") or 0)
except CompShareError as e:
    print("balance failed:", e); sys.exit(1)
rec = {"ts": int(time.time()), "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
       "event": "balance", "balance": bal}
with LEDGER.open("a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print("balance", bal)
