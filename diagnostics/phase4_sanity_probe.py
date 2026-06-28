"""Phase 4 語法與匯入驗證腳本（臨時使用）"""
import io, sys, ast, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')

files = ['core/states.py', 'core/bot.py', 'vision/state_detector.py']
print('=== Phase 4 語法驗證 ===')
ok = True
for f in files:
    src = open(f, encoding='utf-8').read()
    try:
        ast.parse(src)
        print(f'[PASS] {f}')
    except SyntaxError as e:
        print(f'[FAIL] {f}: {e}')
        ok = False

import importlib.util
spec = importlib.util.spec_from_file_location('states', 'core/states.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
expected = [
    'handle_home','handle_lobby','handle_prepare','handle_fast_battle',
    'handle_tap_continue','handle_note_acquired','handle_potential_select',
    'handle_event','handle_shop_choice','handle_shop',
    'handle_explore_complete','handle_result','handle_settlement','handle_reconnect'
]
for fn in expected:
    if hasattr(mod, fn):
        print(f'[PASS] states.{fn}')
    else:
        print(f'[FAIL] states.{fn} not found')
        ok = False

j = json.load(open('data/quiz_answers.json', encoding='utf-8'))
print(f'[PASS] quiz_answers.json version={j["version"]}')

print('=== ALL PASS ===' if ok else '=== FAILURES EXIST ===')

