import json
from pathlib import Path

# 使用相對於此腳本目錄的路徑
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
db_path = WORKSPACE_ROOT / "data" / "priority_list.json"

with open(db_path, "r", encoding="utf-8") as f:
    db = json.load(f)

new_skills = [
    "貓貓拳", "多發彈", "爆裂追擊", "快拳連打", "終結打擊",
    "勇猛挑戰", "回旋反擊", "巔峰狀態",
    "火雨傾盆", "特別裝藥", "童話法則", "慶典再啟", "禮炮雙響", "振奮炮擊",
    "明日·薪火", "薪火的再燃", "伏筆驗證", "客串回", "完美接戲", "視覺衝擊"
]

existing_names = {pot["name"] for pot in db["potentials"]}

for skill in new_skills:
    if skill not in existing_names:
        db["potentials"].append({
            "name": skill,
            "aliases": [skill],
            "tier": "A"
        })

with open(db_path, "w", encoding="utf-8") as f:
    json.dump(db, f, ensure_ascii=False, indent=4)

print("已擴充 databases.")
