
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from sqlalchemy import create_engine, Column, Integer, String, select
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os

# ---------- Konfig ----------
LOCAL_TZ_NAME = os.getenv("TZ", "Europe/Copenhagen")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
UTC = timezone.utc

DB_PATH = os.getenv("DB_PATH", "data/bookings.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# ---------- DB ----------
class Base(DeclarativeBase): pass

class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # UNIX epoch sekunder (UTC)
    start_utc: Mapped[int] = mapped_column(Integer, nullable=False)
    end_utc: Mapped[int] = mapped_column(Integer, nullable=False)

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# ---------- Ressourcer (kan senere flyttes i DB) ----------
RESOURCES = [
    {"id": 1, "name": "Pool 1"},
    {"id": 2, "name": "Pool 2"},
    {"id": 3, "name": "Pool 3"},
    {"id": 4, "name": "Shuffleboard 1"},
]

# ---------- Pydantic ----------
class BookingOut(BaseModel):
    id: int
    resource_id: int
    name: str
    phone: Optional[str]
    start_iso_local: str
    end_iso_local: str

class CreateBookingIn(BaseModel):
    resource_id: int
    name: str
    phone: Optional[str] = None
    date: str                       # "YYYY-MM-DD"
    start_time: str                 # "HH:MM"
    duration_minutes: int = Field(ge=15, le=8*60)

class UpdateBookingIn(BaseModel):
    end_iso_local: Optional[str] = None
    add_minutes: Optional[int] = Field(default=None, ge=1, le=12*60)

# ---------- Hjælpere ----------
def dt_local_to_epoch(d: datetime) -> int:
    """Lokal->UTC epoch sekunder"""
    if d.tzinfo is None:
        d = d.replace(tzinfo=LOCAL_TZ)
    return int(d.astimezone(UTC).timestamp())

def epoch_to_local_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, LOCAL_TZ).isoformat(timespec="seconds")

def overlap(a1:int, a2:int, b1:int, b2:int) -> bool:
    return a1 < b2 and a2 > b1

def resources_dict():
    return {r["id"]: r["name"] for r in RESOURCES}

# ---------- App ----------
app = FastAPI(title="Booking API", version="1.0")

allow_origin = os.getenv("ALLOW_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[allow_origin] if allow_origin != "*" else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "tz": LOCAL_TZ_NAME}

# (optionelt) serve lokale statics hvis du vil
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------- Endpoints ----------
@app.get("/api/resources")
def get_resources():
    return RESOURCES

@app.get("/api/bookings", response_model=List[BookingOut])
def list_bookings(from_: Optional[str] = None, to: Optional[str] = None):
    """
    from_ / to: ISO med tz, fx "2025-08-30T00:00:00+02:00" (valgfrit)
    Hvis ikke sat, returneres alle.
    """
    f_ts = None
    t_ts = None
    if from_:
        f_ts = int(datetime.fromisoformat(from_).astimezone(UTC).timestamp())
    if to:
        t_ts = int(datetime.fromisoformat(to).astimezone(UTC).timestamp())

    db = SessionLocal()
    try:
        q = select(Booking)
        rows = db.execute(q).scalars().all()
        out = []
        for r in rows:
            if f_ts is not None and r.end_utc <= f_ts:
                continue
            if t_ts is not None and r.start_utc >= t_ts:
                continue
            out.append(BookingOut(
                id=r.id,
                resource_id=r.resource_id,
                name=r.name,
                phone=r.phone,
                start_iso_local=epoch_to_local_iso(r.start_utc),
                end_iso_local=epoch_to_local_iso(r.end_utc),
            ))
        return out
    finally:
        db.close()

@app.post("/api/bookings", response_model=BookingOut)
def create_booking(payload: CreateBookingIn):
    # Byg start/slut i lokal tid
    try:
        y, m, d = map(int, payload.date.split("-"))
        sh, sm = map(int, payload.start_time.split(":"))
    except Exception:
        raise HTTPException(400, "Provide 'date' (YYYY-MM-DD) and 'start_time' (HH:MM)")

    start_local = datetime(y, m, d, sh, sm, 0, tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(minutes=payload.duration_minutes)
    # tillad over midnat naturligt

    start_ts = dt_local_to_epoch(start_local)
    end_ts = dt_local_to_epoch(end_local)

    # valider resource
    if payload.resource_id not in resources_dict():
        raise HTTPException(404, "Unknown resource_id")

    db = SessionLocal()
    try:
        # overlap-check på samme resource
        existing = db.execute(select(Booking).where(Booking.resource_id == payload.resource_id)).scalars().all()
        for r in existing:
            if overlap(start_ts, end_ts, r.start_utc, r.end_utc):
                raise HTTPException(409, "Booking overlaps existing reservation")

        row = Booking(
            resource_id=payload.resource_id,
            name=payload.name.strip(),
            phone=(payload.phone or "").strip() or None,
            start_utc=start_ts,
            end_utc=end_ts,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return BookingOut(
            id=row.id,
            resource_id=row.resource_id,
            name=row.name,
            phone=row.phone,
            start_iso_local=epoch_to_local_iso(row.start_utc),
            end_iso_local=epoch_to_local_iso(row.end_utc),
        )
    finally:
        db.close()

@app.delete("/api/bookings/{booking_id}")
def delete_booking(booking_id: int):
    db = SessionLocal()
    try:
        row = db.get(Booking, booking_id)
        if not row:
            raise HTTPException(404, "Booking not found")
        db.delete(row)
        db.commit()
        return JSONResponse({"status": "ok"})
    finally:
        db.close()

@app.put("/api/bookings/{booking_id}", response_model=BookingOut)
def update_booking(booking_id: int, payload: UpdateBookingIn):
    db = SessionLocal()
    try:
        row = db.get(Booking, booking_id)
        if not row:
            raise HTTPException(404, "Booking not found")

        # Beregn ny slut
        if payload.add_minutes is not None:
            new_end_ts = row.end_utc + payload.add_minutes * 60
        elif payload.end_iso_local:
            try:
                dt = datetime.fromisoformat(payload.end_iso_local)
                new_end_ts = int(dt.astimezone(UTC).timestamp())
            except Exception:
                raise HTTPException(400, "end_iso_local must be ISO-8601 with timezone")
        else:
            raise HTTPException(400, "Provide add_minutes or end_iso_local")

        if new_end_ts <= row.start_utc:
            raise HTTPException(400, "New end must be after start")

        # Overlap-check mod andre
        existing = db.execute(select(Booking).where(Booking.resource_id == row.resource_id, Booking.id != row.id)).scalars().all()
        for r in existing:
            if overlap(row.start_utc, new_end_ts, r.start_utc, r.end_utc):
                raise HTTPException(409, "Extension overlaps another booking")

        row.end_utc = new_end_ts
        db.commit()
        db.refresh(row)
        return BookingOut(
            id=row.id,
            resource_id=row.resource_id,
            name=row.name,
            phone=row.phone,
            start_iso_local=epoch_to_local_iso(row.start_utc),
            end_iso_local=epoch_to_local_iso(row.end_utc),
        )
    finally:
        db.close()
