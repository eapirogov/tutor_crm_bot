"""Слой доступа к SQLite. Без ORM — для одного пользователя достаточно sqlite3."""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "tutor.db"
BACKUP_DIR = Path(__file__).parent / "backups"
KEEP_BACKUPS = 14  # сколько последних копий хранить

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    parent_name TEXT,
    contact     TEXT,
    subject     TEXT,
    rate        REAL DEFAULT 0,        -- цена за одно занятие, ₽
    tg_chat_id  INTEGER,              -- chat_id ученика/родителя для напоминаний
    notes       TEXT,
    archived    INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id    INTEGER NOT NULL,
    dt            TEXT NOT NULL,        -- ISO datetime начала занятия
    status        TEXT DEFAULT 'planned', -- planned/done/cancelled/no_show
    price         REAL DEFAULT 0,
    homework      TEXT,
    progress_note TEXT,
    recurrence    TEXT,                 -- напр. "mon,wed 17:00" (NULL для разовых)
    created_at    TEXT NOT NULL,
    FOREIGN KEY (student_id) REFERENCES students(id)
);

-- Каждая запись = покупка пакета (разовое/абонемент).
-- lessons_count добавляет занятия на баланс ученика (в штуках).
CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id    INTEGER NOT NULL,
    dt            TEXT NOT NULL,        -- ISO datetime оплаты
    amount        REAL NOT NULL,        -- сколько заплачено, ₽
    lessons_count INTEGER NOT NULL,     -- сколько занятий добавил пакет
    package       TEXT,                 -- название тарифа (для отчётов)
    comment       TEXT,
    FOREIGN KEY (student_id) REFERENCES students(id)
);

CREATE INDEX IF NOT EXISTS idx_lessons_student ON lessons(student_id);
CREATE INDEX IF NOT EXISTS idx_lessons_dt      ON lessons(dt);
CREATE INDEX IF NOT EXISTS idx_payments_student ON payments(student_id);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def backup_db() -> Path | None:
    """Делает копию БД в backups/ через sqlite backup API. Чистит старые копии."""
    if not DB_PATH.exists():
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    # одна копия в день: имя по дате, перезапишется при повторном старте в тот же день
    dest = BACKUP_DIR / f"tutor_{datetime.now():%Y-%m-%d}.db"
    with sqlite3.connect(DB_PATH) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)
    _cleanup_backups()
    return dest


def _cleanup_backups() -> None:
    """Оставляет только KEEP_BACKUPS свежих копий."""
    backups = sorted(BACKUP_DIR.glob("tutor_*.db"))
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink()


# --- Ученики ---

def add_student(name: str, subject: str = "", rate: float = 0,
                parent_name: str = "", contact: str = "", notes: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO students (name, parent_name, contact, subject, rate, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, parent_name, contact, subject, rate, notes, datetime.now().isoformat()),
        )
        return cur.lastrowid


def get_student(student_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()


def list_students(include_archived: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM students"
    if not include_archived:
        q += " WHERE archived = 0"
    q += " ORDER BY name COLLATE NOCASE"
    with get_conn() as conn:
        return conn.execute(q).fetchall()


def update_student(student_id: int, **fields) -> None:
    if not fields:
        return
    allowed = {"name", "parent_name", "contact", "subject", "rate",
               "tg_chat_id", "notes", "archived"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    with get_conn() as conn:
        conn.execute(f"UPDATE students SET {cols} WHERE id = ?",
                     (*sets.values(), student_id))


def archive_student(student_id: int) -> None:
    update_student(student_id, archived=1)


# --- Занятия ---

LESSON_STATUSES = {
    "planned": "Запланировано",
    "done": "Проведено",
    "cancelled": "Отменено",
    "no_show": "Не пришёл",
}


def add_lesson(student_id: int, dt: str, status: str = "planned") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO lessons (student_id, dt, status, created_at)
               VALUES (?, ?, ?, ?)""",
            (student_id, dt, status, datetime.now().isoformat()),
        )
        return cur.lastrowid


def set_lesson_status(lesson_id: int, status: str) -> None:
    if status not in LESSON_STATUSES:
        raise ValueError(f"Неизвестный статус: {status}")
    with get_conn() as conn:
        conn.execute("UPDATE lessons SET status = ? WHERE id = ?", (status, lesson_id))


def update_lesson(lesson_id: int, dt: str) -> None:
    """Перенос занятия на другое время."""
    with get_conn() as conn:
        conn.execute("UPDATE lessons SET dt = ? WHERE id = ?", (dt, lesson_id))


def delete_lesson(lesson_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))


def lessons_between(start: str, end: str) -> list[sqlite3.Row]:
    """Занятия в диапазоне [start, end) с именем ученика. Границы — ISO datetime."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT l.*, s.name AS student_name, s.subject
               FROM lessons l JOIN students s ON s.id = l.student_id
               WHERE l.dt >= ? AND l.dt < ?
               ORDER BY l.dt""",
            (start, end)).fetchall()


def lessons_for_student(student_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM lessons WHERE student_id = ? ORDER BY dt DESC",
            (student_id,)).fetchall()


def set_lesson_notes(lesson_id: int, homework: str, progress_note: str) -> None:
    """Сохраняет ДЗ и заметку о прогрессе по занятию."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE lessons SET homework = ?, progress_note = ? WHERE id = ?",
            (homework, progress_note, lesson_id))


def add_lessons_bulk(student_id: int, dts: list[str]) -> int:
    """Создаёт пачку запланированных занятий. Возвращает количество."""
    now = datetime.now().isoformat()
    rows = [(student_id, dt, "planned", now) for dt in dts]
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO lessons (student_id, dt, status, created_at) VALUES (?, ?, ?, ?)",
            rows)
    return len(rows)


def lessons_on_day(day_iso: str) -> list[sqlite3.Row]:
    """Занятия за конкретный день (для сводки на главной). day_iso = 'YYYY-MM-DD'."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT l.*, s.name AS student_name, s.subject
               FROM lessons l JOIN students s ON s.id = l.student_id
               WHERE l.dt LIKE ? ORDER BY l.dt""",
            (f"{day_iso}%",)).fetchall()


# --- Тарифы (справочник пакетов) ---

# key: (название, кол-во занятий, цена ₽)
PACKAGES = {
    "single": ("Разовое", 1, 900),
    "pack5": ("Абонемент 5", 5, 4200),
    "pack10": ("Абонемент 10", 10, 8200),
}


# --- Платежи ---

def add_payment(student_id: int, package_key: str, comment: str = "") -> int:
    """Покупка пакета: добавляет занятия на баланс ученика."""
    if package_key not in PACKAGES:
        raise ValueError(f"Неизвестный тариф: {package_key}")
    name, count, price = PACKAGES[package_key]
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO payments (student_id, dt, amount, lessons_count, package, comment)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (student_id, datetime.now().isoformat(), price, count, name, comment),
        )
        return cur.lastrowid


def list_payments(student_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM payments WHERE student_id = ? ORDER BY dt DESC",
            (student_id,)).fetchall()


# --- Баланс (в штуках занятий) ---

def get_balance(student_id: int) -> int:
    """Остаток занятий = куплено пакетами − списанные занятия.

    Списываются проведённые (done) и прогулянные (no_show — занятие сгорает).
    """
    with get_conn() as conn:
        bought = conn.execute(
            "SELECT COALESCE(SUM(lessons_count), 0) FROM payments WHERE student_id = ?",
            (student_id,)).fetchone()[0]
        used = conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE student_id = ? AND status IN ('done', 'no_show')",
            (student_id,)).fetchone()[0]
    return bought - used


def income_for_month(year: int, month: int) -> float:
    """Сумма оплат (₽) за указанный месяц."""
    prefix = f"{year:04d}-{month:02d}"
    with get_conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE dt LIKE ?",
            (f"{prefix}%",)).fetchone()[0]


def income_by_month() -> list[sqlite3.Row]:
    """Доход (₽) и число оплат по месяцам, от свежих к старым."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT substr(dt, 1, 7) AS ym,
                      SUM(amount) AS total,
                      COUNT(*) AS payments
               FROM payments
               GROUP BY ym
               ORDER BY ym DESC""").fetchall()
