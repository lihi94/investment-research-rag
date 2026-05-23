# investment-research-rag

מערכת ידע למחקר השקעות. שלב א': RAG על ספרים פיננסיים. שלב ב' (עתידי): GraphRAG.

## דרישות מערכת (Windows)

מעבר לחבילות Python (ב-`requirements.txt`), המערכת צריכה שני כלי שורת פקודה חיצוניים עבור OCR של PDFs סרוקים:

### 1. Tesseract OCR

מנוע OCR חינמי.

- הורד מ-https://github.com/UB-Mannheim/tesseract/wiki (התקנת Windows)
- בהתקנה: ודא שמסומן "Add to PATH"
- בחר גם מודלי שפה: English (ברירת מחדל) **ו-Hebrew**
- אחרי ההתקנה, פתח PowerShell חדש והרץ `tesseract --version` — חייב לעבוד

### 2. Poppler

ספרייה שצריך כדי להמיר עמודי PDF לתמונות (לפני שמעבירים ל-Tesseract).

- הורד מ-https://github.com/oschwartz10612/poppler-windows/releases (גרסה אחרונה)
- חלץ ל-`C:\Program Files\poppler\`
- הוסף `C:\Program Files\poppler\Library\bin` ל-PATH של Windows

## התקנה

```powershell
cd "C:\Users\eliha\OneDrive\Desktop\cluade code\investment-research-rag"

# יצירת סביבה וירטואלית — שכבת בידוד לחבילות של הפרויקט
python -m venv .venv
.venv\Scripts\activate

# התקנת החבילות
pip install -r requirements.txt

# העתקת תבנית המפתחות ומילוי הערכים
Copy-Item .env.example .env
notepad .env   # מלא את 3 המפתחות
```

## הרצה

הסקריפטים מספרי לפי סדר ההרצה הצפוי:

```powershell
# סקר ראשוני — סופר PDFs ומעריך עלות. לא נוגע במסד הנתונים.
python scripts/01_survey.py

# קליטה לאחר אישור הדוח של הסקר. Resumable — אפשר להפסיק ולהמשיך.
python scripts/02_ingest.py

# שאילתות אינטראקטיביות. תשובות עם ציטוטים (ספר + עמוד).
python scripts/03_query.py
```

## מבנה תיקיות

```
db/schema.sql         סכמת מסד הנתונים (להעתיק ל-SQL Editor של Supabase)
src/                  מודולי Python לשימוש חוזר (config, db, pdf, chunker, embeddings)
scripts/              נקודות כניסה: 01_survey, 02_ingest, 03_query
data/pdfs/            תיקיית הקלט של ה-PDFs (לא נכנסת ל-git)
logs/                 פלט הקליטה (לא נכנסת ל-git)
```
