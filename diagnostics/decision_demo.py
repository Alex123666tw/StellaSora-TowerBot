"""
決策引擎測試腳本 (test_decision.py)

模擬多種「潛能三選一」情境，驗證 DecisionEngine 的決策邏輯是否符合規範。
包含規則 1-5b（原始邏輯）以及規則 6（累計等級限量必選）的完整情境。
"""
import sys
import os
import logging

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from _bootstrap import PROJECT_ROOT
from core.decision_engine import DecisionEngine, ScreenOption

logging.basicConfig(level=logging.INFO, format="  %(message)s")


# ── 輔助函式 ──────────────────────────────────────────────────

def opt(name: str, gain: int, pos=(0, 0)) -> ScreenOption:
    """建立 ScreenOption，gain 為此卡提供的升等量（+1/+2/+3）。"""
    return ScreenOption(name=name, level=gain, position=pos)

def run(title: str, engine: DecisionEngine, options: list[ScreenOption]):
    """執行情境並印出狀態與結果。"""
    print(f"\n{'─'*55}")
    print(f"🎮 {title}")
    acc = {k: v for k, v in engine.state.accumulated_levels.items()}
    print(f"   累計等級: {acc if acc else '（空）'}, reroll={engine.state.reroll_count}")
    options_summary = [(o.name, f'+{o.level}') for o in options]
    print(f"   選項: {options_summary}")

    result = engine.decide(options)
    if result:
        new_lv = engine.state.current_level(result.name)
        print(f"   ✅ 選擇「{result.name}」+{result.level} → 累計 Lv.{new_lv} [{result.category}]")
    else:
        print(f"   🔄 Reroll（reroll_count 現在={engine.state.reroll_count}）")


# ── 主測試流程 ────────────────────────────────────────────────

def main():
    print("═" * 55)
    print("   星塔旅人：潛能決策引擎邏輯測試（累計版）")
    print("═" * 55)

    engine = DecisionEngine(config_path="config.yaml")
    print(f"\n📋 設定載入：")
    print(f"   必選（目標Lv.6）：{engine._required}")
    print(f"   限量必選：{engine._level_required}")
    print(f"   備選：{engine._backup}")

    # ══ 規則 1~3：必選累計升等 ═══════════════════════════════
    print("\n\n【測試組 A：required 累計升到 Lv.6】")

    run("選必選潛能 +2（累計 0→2）", engine,
        [opt("攻擊力提升", 2), opt("防禦力提升", 1), opt("薪火的再燃", 1)])

    run("再選必選 +3（累計 2→5）", engine,
        [opt("攻擊力提升", 3), opt("防禦力提升", 2), opt("薪火的再燃", 1)])

    run("再選必選 +1（累計 5→6，到達 Lv.6 滿等）", engine,
        [opt("攻擊力提升", 1), opt("防禦力提升", 1), opt("暴擊率強化", 2)])

    run("攻擊力已達 Lv.6，轉選暴擊率強化 +2（規則 1：選未滿的必選）", engine,
        [opt("攻擊力提升", 1), opt("暴擊率強化", 2), opt("薪火的再燃", 1)])

    # ══ 規則 6：level_required 累計升等 ══════════════════════
    print("\n\n【測試組 B：level_required 目標等級邏輯】")
    engine.reset_state()

    run("與刃共舞目標Lv.5，出現+2（累計0→2，還沒到5，應選）", engine,
        [opt("與刃共舞", 2), opt("薪火的再燃", 1), opt("冰霜護盾", 1)])

    run("與刃共舞再出現+3（累計2→5，達到Lv.5目標，應選）", engine,
        [opt("與刃共舞", 3), opt("薪火的再燃", 1), opt("冰霜護盾", 1)])

    run("與刃共舞已達目標Lv.5，再出現應忽略（因為已達標，降為unknown→Reroll）", engine,
        [opt("與刃共舞", 1), opt("薪火的再燃", 1), opt("冰霜護盾", 1)])

    engine.reset_state()
    run("疾速拔刀目標Lv.1，出現+3（雖然給3等，只要累計>=1就算達標，應先選）", engine,
        [opt("疾速拔刀", 3), opt("薪火的再燃", 1), opt("冰霜護盾", 1)])

    run("疾速拔刀已達Lv.1（累計已到3）→ 再出現應忽略→Reroll", engine,
        [opt("疾速拔刀", 1), opt("薪火的再燃", 1), opt("冰霜護盾", 1)])

    # ══ 規則優先序 ════════════════════════════════════════════
    print("\n\n【測試組 C：優先序驗證 required > level_req > backup】")
    engine.reset_state()

    run("必選 + 限量必選並存 → 必選優先", engine,
        [opt("攻擊力提升", 2), opt("與刃共舞", 3), opt("防禦力提升", 1)])

    run("無必選，有限量必選(未達標) + 備選 → 限量必選優先", engine,
        [opt("與刃共舞", 2), opt("防禦力提升", 1), opt("薪火的再燃", 1)])

    # ══ 規則 5/5a：Reroll 達上限 ═════════════════════════════
    print("\n\n【測試組 D：Reroll 達上限，備選降級】")
    engine.reset_state()
    for i in range(3):
        run(f"全 unknown（第{i+1}次Reroll）", engine,
            [opt("薪火的再燃", 1), opt("冰霜護盾", 1), opt("烈焰爆發", 1)])

    run("Reroll達上限，出現備選 → 降級選備選", engine,
        [opt("薪火的再燃", 1), opt("防禦力提升", 2), opt("生命值強化", 1)])

    print(f"\n{'═'*55}")
    print("   全部情境測試完畢！")
    print("═" * 55)


if __name__ == "__main__":
    main()

