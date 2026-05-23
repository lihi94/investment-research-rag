-- =============================================================================
-- investment-research-rag : schema.sql
-- =============================================================================
-- מטרה: מסד נתונים לשלב א' (RAG) שמוכן כבר לשלב ב' (GraphRAG) בלי הגירה כואבת.
--
-- המבנה:
--   sources        - רשומה לכל מסמך מקור (ספר PDF, ובעתיד גם APIs).
--   chunks         - קטעי טקסט עם embeddings. הלב של מערכת ה-RAG.
--   entities       - ישויות גרף הידע (ריק עכשיו - יתמלא בשלב ב').
--   relationships  - קשרים בין ישויות (ריק עכשיו - יתמלא בשלב ב').
--
-- להרצה: העתק את כל הקובץ הזה ל-SQL Editor של Supabase ולחץ Run.
-- בטוח להרצה חוזרת (idempotent) - כל הפקודות משתמשות ב-IF NOT EXISTS.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. הפעלת תוספים (extensions)
-- -----------------------------------------------------------------------------
-- pgvector: מוסיף סוג נתונים vector ואינדקסים מהירים לחיפוש דמיון.
-- בלעדיו אי אפשר לעשות RAG.
CREATE EXTENSION IF NOT EXISTS vector;

-- pgcrypto: נחוץ ליצירת UUIDים אקראיים (gen_random_uuid).
-- ה-UUID של כל chunk הוא המזהה היציב שעליו מצביעים מהגרף בשלב ב'.
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- -----------------------------------------------------------------------------
-- 2. טבלת sources - רשומה אחת לכל מסמך מקור
-- -----------------------------------------------------------------------------
-- מתוכננת להכיל יותר מסתם ספרי PDF. בעתיד אפשר להוסיף source_type='api_macro'
-- (סדרות מאקרו), 'api_options' (נתוני אופציות) וכו' בלי לשנות את שאר הסכמה.
CREATE TABLE IF NOT EXISTS sources (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type     TEXT            NOT NULL,    -- 'pdf_book' | 'api_macro' | 'api_options' | ...
    title           TEXT            NOT NULL,    -- שם הספר / מקור
    author          TEXT,                        -- מחבר (אם רלוונטי)
    file_path       TEXT,                        -- הנתיב המקומי לקובץ המקור (לקבצי PDF)
    file_hash       TEXT            UNIQUE,      -- SHA256 של הקובץ - מונע קליטה כפולה
    metadata        JSONB           DEFAULT '{}'::jsonb,  -- שדה חופשי לעוד metadata (שנת הוצאה וכו')
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    ingested_at     TIMESTAMPTZ                  -- מתי הקליטה הסתיימה. NULL = בתהליך / נכשלה.
);

-- אינדקס על source_type לחיפוש מהיר ("כל הספרים", "כל מקורות המאקרו")
CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(source_type);

-- אינדקס על file_hash לבדיקה מהירה אם קובץ כבר נקלט - קריטי ל-resumable ingestion
CREATE INDEX IF NOT EXISTS idx_sources_file_hash ON sources(file_hash);


-- -----------------------------------------------------------------------------
-- 3. טבלת chunks - קטעי הטקסט. הלב של מערכת ה-RAG.
-- -----------------------------------------------------------------------------
-- כל chunk הוא בערך 500-800 מילים (נקבע בקליטה).
-- ה-embedding הוא וקטור של 1536 ערכים (התואם ל-text-embedding-3-small של OpenAI).
-- ה-id הוא יציב לנצח - בשלב ב', כל קשר בגרף יצביע ל-chunk הזה כראיה.
CREATE TABLE IF NOT EXISTS chunks (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       UUID            NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    chunk_index     INT             NOT NULL,    -- מספר רץ של הקטע בתוך המסמך (0, 1, 2, ...)
    page_number     INT,                         -- מספר העמוד שממנו התחיל הקטע (NULL אם לא ידוע)
    content         TEXT            NOT NULL,    -- הטקסט עצמו
    word_count      INT,                         -- ספירת מילים (לסטטיסטיקה ולפילוח)
    embedding       VECTOR(1536),                -- הוקטור של text-embedding-3-small
    metadata        JSONB           DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),

    -- אילוץ ייחודיות: בתוך מסמך, אסור שיהיו שני chunks עם אותו chunk_index
    UNIQUE (source_id, chunk_index)
);

-- אינדקס על source_id - חיפוש "כל הקטעים של ספר X" יהיה מהיר
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);

-- אינדקס דמיון וקטורי באמצעות HNSW (Hierarchical Navigable Small World).
-- HNSW מהיר יותר ומדויק יותר מ-ivfflat בנתונים בגודל שלנו (עשרות-מאות אלפי קטעים).
-- vector_cosine_ops = מדידת דמיון cosine, סטנדרט ל-OpenAI embeddings.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks
    USING hnsw (embedding vector_cosine_ops);


-- -----------------------------------------------------------------------------
-- 4. טבלאות לשלב ב' (GraphRAG) - נוצרות עכשיו, ריקות עד שלב ב'
-- -----------------------------------------------------------------------------
-- entities: ישויות הגרף - מושגים, עקרונות, ישויות פיננסיות.
-- לדוגמה: "Margin of Safety", "Discounted Cash Flow", "Federal Reserve".
-- כל ישות נחלצת מ-chunk ספציפי (source_chunk_id) או צוברת ראיות ממספר chunks.
CREATE TABLE IF NOT EXISTS entities (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT            NOT NULL,    -- שם קנוני של הישות
    entity_type     TEXT,                        -- 'concept' | 'principle' | 'person' | 'institution' | ...
    description     TEXT,                        -- הסבר מאוחד שנוצר על ידי LLM
    embedding       VECTOR(1536),                -- embedding על תיאור הישות (לחיפוש דמיון בין ישויות)
    metadata        JSONB           DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),

    UNIQUE (name, entity_type)  -- אסור כפילות של אותה ישות מאותו סוג
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_embedding ON entities
    USING hnsw (embedding vector_cosine_ops);


-- relationships: קשרים בין ישויות.
-- לדוגמה: ("Margin of Safety") --SUPPORTS--> ("Value Investing")
--          ("EMH") --CONTRADICTS--> ("Active Management")
-- כל קשר מצביע ל-chunk המקור (source_chunk_id) - שם הראיה שתומכת בקשר.
CREATE TABLE IF NOT EXISTS relationships (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_entity_id    UUID        NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id    UUID        NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type       TEXT        NOT NULL,    -- 'supports' | 'contradicts' | 'depends_on' | 'part_of' | ...
    source_chunk_id     UUID        REFERENCES chunks(id) ON DELETE SET NULL,  -- ה-chunk שממנו נחלץ הקשר
    confidence          REAL,                    -- ביטחון של ה-LLM בחילוץ (0.0-1.0)
    description         TEXT,                    -- הסבר חופשי על הקשר
    metadata            JSONB       DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_relationships_source_entity ON relationships(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target_entity ON relationships(target_entity_id);
CREATE INDEX IF NOT EXISTS idx_relationships_chunk ON relationships(source_chunk_id);
CREATE INDEX IF NOT EXISTS idx_relationships_type ON relationships(relation_type);


-- -----------------------------------------------------------------------------
-- 5. פונקציית חיפוש דמיון - הלב של ה-RAG
-- -----------------------------------------------------------------------------
-- מקבלת: שאילתת embedding, סף דמיון מינימלי, וכמה תוצאות להחזיר.
-- מחזירה: ה-chunks הכי דומים, יחד עם metadata של המקור (כדי לציטוט).
-- נקראת מ-Python דרך client.rpc('match_chunks', {...}).
CREATE OR REPLACE FUNCTION match_chunks(
    query_embedding     VECTOR(1536),
    match_threshold     REAL    DEFAULT 0.5,    -- מתחת לסף הזה - לא רלוונטי
    match_count         INT     DEFAULT 8       -- כמה קטעים להחזיר
)
RETURNS TABLE (
    chunk_id        UUID,
    source_id       UUID,
    source_title    TEXT,
    source_author   TEXT,
    page_number     INT,
    chunk_index     INT,
    content         TEXT,
    similarity      REAL
)
LANGUAGE SQL
STABLE
AS $$
    SELECT
        c.id                                                AS chunk_id,
        c.source_id,
        s.title                                             AS source_title,
        s.author                                            AS source_author,
        c.page_number,
        c.chunk_index,
        c.content,
        (1 - (c.embedding <=> query_embedding))::REAL       AS similarity
    FROM chunks c
    JOIN sources s ON s.id = c.source_id
    WHERE c.embedding IS NOT NULL
      AND (1 - (c.embedding <=> query_embedding)) >= match_threshold
    ORDER BY c.embedding <=> query_embedding   -- <=> = מרחק cosine (קטן יותר = דומה יותר)
    LIMIT match_count;
$$;


-- -----------------------------------------------------------------------------
-- 6. תצוגות עזר (views) לבדיקות יומיומיות
-- -----------------------------------------------------------------------------
-- ספירה מהירה: כמה chunks יש לכל מקור, האם כולם קיבלו embedding.
CREATE OR REPLACE VIEW source_stats AS
SELECT
    s.id,
    s.source_type,
    s.title,
    s.author,
    s.ingested_at,
    COUNT(c.id)                                     AS total_chunks,
    COUNT(c.embedding)                              AS embedded_chunks,
    COUNT(c.id) - COUNT(c.embedding)                AS pending_embeddings
FROM sources s
LEFT JOIN chunks c ON c.source_id = s.id
GROUP BY s.id, s.source_type, s.title, s.author, s.ingested_at
ORDER BY s.created_at DESC;


-- -----------------------------------------------------------------------------
-- 7. RLS (Row Level Security)
-- -----------------------------------------------------------------------------
-- ב-Supabase, אם RLS מופעל ואין policies - הטבלה חסומה לקריאה ציבורית.
-- אנחנו ניגשים דרך service_role key (שעוקף RLS לחלוטין), אז זה בדיוק מה שאנחנו רוצים:
-- אף אחד לא יכול לקרוא את הנתונים דרך ה-API הציבורי, רק הקוד שלנו עם המפתח הסודי.
ALTER TABLE sources       ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks        ENABLE ROW LEVEL SECURITY;
ALTER TABLE entities      ENABLE ROW LEVEL SECURITY;
ALTER TABLE relationships ENABLE ROW LEVEL SECURITY;


-- -----------------------------------------------------------------------------
-- 8. בדיקה אחרונה - אמור להחזיר 4 טבלאות
-- -----------------------------------------------------------------------------
-- אחרי הרצה, הרץ את השאילתה הבאה לוודא שהכל נוצר:
--
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public' ORDER BY table_name;
--
-- אמור להחזיר: chunks, entities, relationships, sources
-- =============================================================================
