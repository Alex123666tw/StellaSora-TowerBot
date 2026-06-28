# 貢獻指南

歡迎為 **星塔旅人自動爬塔工具** 貢獻一份心力。

本專案歡迎各種形式的參與:回報問題、改善文件、提出新點子,或直接送出程式碼。

---

## 回報問題

開立 issue 前,請先:

1. 搜尋既有 issue,避免重複。
2. 提供清楚的重現步驟、預期行為與實際行為。
3. 附上環境資訊:作業系統版本、Python 版本、遊戲解析度與視窗模式。
4. 盡量附上相關日誌(例如 `bot.log`)或截圖。
5. 若為**安全性**問題,請改依 [SECURITY.md](SECURITY.md) 私下回報,**勿** 開公開 issue。

## 開發環境設定

```bash
# 1. Fork 並 clone 此專案
git clone https://github.com/<your-account>/<your-fork>.git
cd 星塔旅人

# 2. 建立虛擬環境
python -m venv .venv
.venv\Scripts\activate

# 3. 安裝開發相依套件(含 pytest)
pip install -r requirements-dev.txt
```

> 本專案於 **Windows 10 / 11 + Python 3.8+** 環境開發。部分功能(視窗擷取、輸入模擬)需要系統管理員權限,且依賴 Windows API。

## 測試

提交前請先執行測試,並確保通過:

```bash
python -m pytest -q tests
```

新增功能或修正錯誤時,請盡量補上對應的測試。本專案採用「回放測試」模式(以截圖語料回放狀態機),可參考 `tests/` 目錄中的既有寫法。

## 提交 Pull Request

1. 從最新的預設分支建立功能分支:`git checkout -b feat/your-feature`。
2. 進行修改,保持每個 commit 聚焦、可理解。
3. 執行測試並確保通過。
4. 推送分支並開立 Pull Request,於描述中說明動機、做法與測試方式。
5. 若 PR 對應某個 issue,請在描述中連結(例如 `Closes #123`)。

## Commit 訊息慣例

建議採用 [Conventional Commits](https://www.conventionalcommits.org/) 格式:

```
<type>(<scope>): <subject>
```

常用 `type`:`feat`(新功能)、`fix`(修正)、`docs`(文件)、`refactor`(重構)、`test`(測試)、`chore`(雜項)。

範例:

```
feat(decision): 潛能卡新增重抽降級備選
fix(shop): 避免重複進入已清空的商店
docs(readme): 釐清系統管理員權限需求
```

## 程式風格

- 遵循 **PEP 8**,並儘量加上型別註記。
- 所有檔案使用 **UTF-8** 編碼。
- 命名與註解清楚易懂;對外行為的變更請一併更新相關文件。
- 提交前可用 `python -m py_compile <files...>` 做基本語法檢查。
