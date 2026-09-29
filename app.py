"""CRM для учёта занятий репетитора. FastAPI + Jinja2 + HTMX, SQLite."""
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def contact_link(value: str):
    """Делает контакт кликабельным: @username → Telegram, цифры → tel:."""
    from markupsafe import Markup, escape
    v = value.strip()
    safe = escape(v)
    if v.startswith("@"):
        return Markup(f'<a class="contact-link" href="https://t.me/{escape(v[1:])}">{safe}</a>')
    digits = v.lstrip("+").replace(" ", "").replace("-", "")
    if digits.isdigit() and len(digits) >= 6:
        tel = "+" + digits if v.startswith("+") else digits
        return Markup(f'<a class="contact-link" href="tel:{escape(tel)}">{safe}</a>')
    return safe


templates.env.globals["contact_link"] = contact_link

app = FastAPI(title="Tutor CRM")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.backup_db()   # копия текущего состояния перед работой
    db.init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    today = date.today()
    today_lessons = db.lessons_on_day(today.isoformat())
    debtors = [_student_row(s) for s in db.list_students()
               if db.get_balance(s["id"]) <= 0]
    return templates.TemplateResponse(
        request, "home.html",
        {"today_label": today.strftime("%d.%m.%Y"),
         "lessons": today_lessons, "debtors": debtors,
         "statuses": db.LESSON_STATUSES})


# --- Ученики ---

@app.get("/students", response_class=HTMLResponse)
def students_page(request: Request):
    students = db.list_students()
    rows = [_student_row(s) for s in students]
    return templates.TemplateResponse(
        request, "students.html", {"students": rows})


@app.post("/students", response_class=HTMLResponse)
def students_add(
    request: Request,
    name: str = Form(...),
    subject: str = Form(""),
    rate: float = Form(0),
    parent_name: str = Form(""),
    contact: str = Form(""),
):
    db.add_student(name=name.strip(), subject=subject.strip(), rate=rate,
                   parent_name=parent_name.strip(), contact=contact.strip())
    # HTMX подменит таблицу новым списком
    students = db.list_students()
    rows = [_student_row(s) for s in students]
    return templates.TemplateResponse(
        request, "_students_table.html", {"students": rows})


@app.get("/students/{student_id}", response_class=HTMLResponse)
def student_detail(request: Request, student_id: int):
    student = db.get_student(student_id)
    if student is None:
        return RedirectResponse("/students")
    return templates.TemplateResponse(
        request, "student_detail.html",
        {"s": student, "balance": db.get_balance(student_id),
         "payments": db.list_payments(student_id), "packages": db.PACKAGES,
         "lessons": db.lessons_for_student(student_id), "statuses": db.LESSON_STATUSES})


@app.post("/students/{student_id}/edit")
def student_edit(
    student_id: int,
    name: str = Form(...),
    subject: str = Form(""),
    parent_name: str = Form(""),
    contact: str = Form(""),
    notes: str = Form(""),
):
    db.update_student(student_id, name=name.strip(), subject=subject.strip(),
                      parent_name=parent_name.strip(), contact=contact.strip(),
                      notes=notes.strip())
    return RedirectResponse(f"/students/{student_id}", status_code=303)


@app.post("/students/{student_id}/archive")
def student_archive(student_id: int):
    db.archive_student(student_id)
    return RedirectResponse("/students", status_code=303)


# --- Деньги ---

@app.post("/students/{student_id}/payment")
def add_payment(student_id: int, package: str = Form(...)):
    db.add_payment(student_id, package)
    return RedirectResponse(f"/students/{student_id}", status_code=303)


MONTH_NAMES = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
               "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


@app.get("/money", response_class=HTMLResponse)
def money_page(request: Request):
    months = [{"label": _month_label(r["ym"]), "total": r["total"],
               "payments": r["payments"]} for r in db.income_by_month()]
    total = sum(m["total"] for m in months)
    debtors = [_student_row(s) for s in db.list_students()
               if db.get_balance(s["id"]) <= 0]
    return templates.TemplateResponse(
        request, "money.html",
        {"months": months, "total": total, "debtors": debtors})


def _month_label(ym: str) -> str:
    """'2026-06' → 'Июнь 2026'."""
    y, m = ym.split("-")
    return f"{MONTH_NAMES[int(m)]} {y}"


# --- Расписание ---

@app.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, offset: int = 0):
    """offset — сдвиг недель относительно текущей (0=эта, -1=прошлая, 1=следующая)."""
    monday = date.today() - timedelta(days=date.today().weekday()) + timedelta(weeks=offset)
    week = _build_week(monday)
    return templates.TemplateResponse(
        request, "schedule.html",
        {"hours": week["hours"], "days": week["days"], "hour_px": week["hour_px"],
         "offset": offset,
         "week_label": f"{monday.strftime('%d.%m')} – {(monday + timedelta(days=6)).strftime('%d.%m.%Y')}",
         "students": db.list_students(), "statuses": db.LESSON_STATUSES})


@app.post("/lessons")
def lesson_add(student_id: int = Form(...), dt: str = Form(...), offset: int = Form(0)):
    # dt приходит из <input type="datetime-local"> в формате 'YYYY-MM-DDTHH:MM'
    db.add_lesson(student_id, dt)
    return RedirectResponse(f"/schedule?offset={offset}", status_code=303)


@app.post("/lessons/recurring")
def lessons_recurring(
    student_id: int = Form(...),
    weekdays: list[int] = Form(...),   # 0=Пн … 6=Вс
    time: str = Form(...),             # 'HH:MM'
    weeks: int = Form(...),
    offset: int = Form(0),
):
    dts = _recurring_dates(weekdays, time, weeks)
    db.add_lessons_bulk(student_id, dts)
    return RedirectResponse(f"/schedule?offset={offset}", status_code=303)


@app.post("/lessons/{lesson_id}/status")
def lesson_status(lesson_id: int, status: str = Form(...), offset: int = Form(0)):
    db.set_lesson_status(lesson_id, status)
    return RedirectResponse(f"/schedule?offset={offset}", status_code=303)


@app.post("/lessons/{lesson_id}/delete")
def lesson_delete(lesson_id: int, offset: int = Form(0)):
    db.delete_lesson(lesson_id)
    return RedirectResponse(f"/schedule?offset={offset}", status_code=303)


@app.post("/lessons/{lesson_id}/notes")
def lesson_notes(lesson_id: int, student_id: int = Form(...),
                 homework: str = Form(""), progress_note: str = Form("")):
    db.set_lesson_notes(lesson_id, homework.strip(), progress_note.strip())
    return RedirectResponse(f"/students/{student_id}", status_code=303)


def _recurring_dates(weekdays: list[int], time_str: str, weeks: int) -> list[str]:
    """Список ISO datetime для повторов: заданные дни недели × N недель, с этого понедельника."""
    hh, mm = (int(x) for x in time_str.split(":"))
    monday = date.today() - timedelta(days=date.today().weekday())
    out = []
    for w in range(weeks):
        for wd in weekdays:
            d = monday + timedelta(weeks=w, days=wd)
            slot = datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm)
            if slot >= datetime.now():  # не плодим прошедшие слоты
                out.append(slot.strftime("%Y-%m-%dT%H:%M"))
    return sorted(out)


DAY_START, DAY_END = 8, 22  # базовые границы видимой сетки по часам
HOUR_PX = 58                # высота одного часа в пикселях (см. .cal-cell в CSS)
LESSON_MIN = 60             # длительность занятия, мин (пока фиксированная)


def _build_week(monday: date) -> dict:
    """Сетка с абсолютным позиционированием занятий по времени.

    Каждое занятие получает top/height в px, поэтому слот может пересекать
    границу часа (напр. 13:30–14:30).
    """
    start = datetime.combine(monday, datetime.min.time())
    end = start + timedelta(days=7)
    lessons = db.lessons_between(start.isoformat(), end.isoformat())

    # Диапазон часов сетки расширяем под крайние занятия
    starts = [(int(l["dt"][11:13]), int(l["dt"][14:16])) for l in lessons]
    low = min([DAY_START] + [h for h, _ in starts])
    # верхняя граница — с учётом конца самого позднего занятия
    ends_h = [DAY_END] + [h + (m + LESSON_MIN + 59) // 60 for h, m in starts]
    high = max(ends_h)
    hours = list(range(low, high))

    by_day: dict[str, list] = {}
    for l in lessons:
        h, m = int(l["dt"][11:13]), int(l["dt"][14:16])
        item = dict(l)
        item["hh_mm"] = l["dt"][11:16]
        item["end_hh_mm"] = _add_minutes(h, m, LESSON_MIN)
        # позиция от начала сетки (low) в пикселях
        item["top"] = round(((h - low) * 60 + m) / 60 * HOUR_PX)
        item["height"] = round(LESSON_MIN / 60 * HOUR_PX)
        by_day.setdefault(l["dt"][:10], []).append(item)

    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        days.append({"date": d, "label": names[i], "is_today": d == date.today(),
                     "events": by_day.get(d.isoformat(), [])})
    return {"hours": hours, "days": days, "hour_px": HOUR_PX}


def _add_minutes(h: int, m: int, delta: int) -> str:
    total = h * 60 + m + delta
    return f"{total // 60 % 24:02d}:{total % 60:02d}"


def _student_row(s) -> dict:
    """Сборка строки для таблицы с балансом."""
    d = dict(s)
    d["balance"] = db.get_balance(s["id"])
    return d
