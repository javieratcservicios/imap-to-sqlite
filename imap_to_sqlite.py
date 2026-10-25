#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
imap_to_sqlite.py — Sincroniza uno o varios buzones IMAP a una base SQLite local.

Diseño:
  * Sin dependencias externas (solo biblioteca estándar).
  * Credenciales NUNCA en el código: se leen de variables de entorno.
  * Sincronización incremental por UID, idempotente y re-ejecutable.
  * Guarda cuerpo (texto y HTML), cabeceras y adjuntos en disco.

Uso:
    export IMAP_PASS_MI_CUENTA="xxxx xxxx xxxx xxxx"
    python imap_to_sqlite.py --config accounts.json --status
    python imap_to_sqlite.py --config accounts.json --days 30
    python imap_to_sqlite.py --config accounts.json --all

El archivo accounts.json describe las cuentas:

    [
      {
        "email": "yo@dominio.com",
        "password_env": "IMAP_PASS_MI_CUENTA",
        "imap_server": "imap.gmail.com",
        "imap_port": 993,
        "folders": ["INBOX"],
        "label": "Personal"
      }
    ]
"""
from __future__ import annotations

import argparse
import email
import hashlib
import imaplib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

# En Windows la consola suele usar cp1252 y truena al imprimir acentos o
# símbolos de caja. Forzamos UTF-8 cuando el stream lo permita.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT_DATA_DIR = Path(os.environ.get("MAIL_DATA_DIR", Path.home() / "mailbox"))
DEFAULT_DB = DEFAULT_DATA_DIR / "mail.db"
CHUNK_SIZE = 50           # correos por lote (Gmail corta si pides demasiados)
PAUSE_BETWEEN_CHUNKS = 0.5


# ───────────────────────────── Utilidades ──────────────────────────────

def decode_mime(value) -> str:
    """Decodifica una cabecera MIME (puede venir partida o codificada)."""
    if not value:
        return ""
    if isinstance(value, tuple):
        value = " ".join(str(v) for v in value if v)
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            try:
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return " ".join(parts).strip()


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def split_address(header_value) -> tuple[str, str]:
    """Devuelve (nombre, correo) a partir de una cabecera From/To."""
    full = decode_mime(header_value)
    match = EMAIL_RE.search(full)
    if not match:
        return full, ""
    name = re.sub(r"\s*<[^>]+>\s*", "", full).strip().strip('"')
    return name, match.group(0)


def safe_filename(name: str, max_len: int = 180) -> str:
    """Sanea un nombre de archivo para que sea válido en cualquier SO."""
    if not name:
        return "unnamed"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        stem, ext = os.path.splitext(name)
        ext = ext[:20]
        name = stem[: max_len - len(ext) - 1] + ext
    return name or "unnamed"


def parse_date(raw) -> datetime | None:
    """Convierte una cabecera Date a datetime naive en hora local."""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    except (TypeError, ValueError, OverflowError):
        return None


# ───────────────────────────── Base de datos ───────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT UNIQUE NOT NULL,
    label             TEXT,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_sync         DATETIME,
    total_emails      INTEGER DEFAULT 0,
    total_attachments INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS emails (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL,
    uid              INTEGER NOT NULL,
    folder           TEXT DEFAULT 'INBOX',
    message_id       TEXT,
    sender           TEXT,
    sender_name      TEXT,
    sender_email     TEXT,
    recipients       TEXT,
    cc               TEXT,
    reply_to         TEXT,
    subject          TEXT,
    date             DATETIME,
    body_plain       TEXT,
    body_html        TEXT,
    attachment_count INTEGER DEFAULT 0,
    raw_size         INTEGER DEFAULT 0,
    flags            TEXT,
    eml_filename     TEXT,
    downloaded_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, uid, folder),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id     INTEGER NOT NULL,
    account_id   INTEGER NOT NULL,
    email_uid    INTEGER,
    filename     TEXT NOT NULL,
    content_type TEXT,
    content_id   TEXT,
    size         INTEGER DEFAULT 0,
    filepath     TEXT,
    downloaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_emails_account ON emails(account_id);
CREATE INDEX IF NOT EXISTS idx_emails_uid     ON emails(account_id, uid);
CREATE INDEX IF NOT EXISTS idx_emails_date    ON emails(date);
CREATE INDEX IF NOT EXISTS idx_emails_sender  ON emails(sender_email);
CREATE INDEX IF NOT EXISTS idx_att_email      ON attachments(email_id);

-- Búsqueda de texto completo sobre asunto y cuerpo (SQLite FTS5).
CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
    subject, body_plain, content='emails', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, subject, body_plain)
    VALUES (new.id, new.subject, new.body_plain);
END;

CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, body_plain)
    VALUES ('delete', old.id, old.subject, old.body_plain);
END;
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ───────────────────────────── Sincronizador ───────────────────────────

class MailboxSync:
    """Sincroniza un buzón IMAP a SQLite."""

    def __init__(self, account: dict, conn: sqlite3.Connection,
                 data_dir: Path, since_days: int = 30,
                 fetch_all: bool = False, save_eml: bool = False):
        self.cfg = account
        self.email_addr = account["email"]
        self.password = resolve_password(account)
        self.server = account.get("imap_server", "imap.gmail.com")
        self.port = int(account.get("imap_port", 993))
        self.label = account.get("label") or self.email_addr
        self.folders = account.get("folders") or ["INBOX"]
        self.conn = conn
        self.data_dir = data_dir
        self.since_days = since_days
        self.fetch_all = fetch_all
        self.save_eml = save_eml

        self.account_id: int | None = None
        self.imap: imaplib.IMAP4_SSL | None = None
        self.new_emails = 0
        self.new_attachments = 0

        self.attach_dir = data_dir / sanitize_account_dir(self.email_addr) / "attachments"
        self.raw_dir = data_dir / sanitize_account_dir(self.email_addr) / "raw"
        self.attach_dir.mkdir(parents=True, exist_ok=True)
        if save_eml:
            self.raw_dir.mkdir(parents=True, exist_ok=True)

    # ---- IMAP ----
    def connect(self):
        self.imap = imaplib.IMAP4_SSL(self.server, self.port)
        self.imap.login(self.email_addr, self.password)
        return self.imap

    def disconnect(self):
        if not self.imap:
            return
        try:
            self.imap.logout()
        except Exception:
            pass

    # ---- Cuenta ----
    def ensure_account(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM accounts WHERE email = ?", (self.email_addr,))
        row = cur.fetchone()
        if row:
            self.account_id = row["id"]
        else:
            cur.execute(
                "INSERT INTO accounts (email, label) VALUES (?, ?)",
                (self.email_addr, self.label),
            )
            self.conn.commit()
            self.account_id = cur.lastrowid
        return self.account_id

    def known_uids(self, folder: str) -> set[int]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT uid FROM emails WHERE account_id = ? AND folder = ?",
            (self.account_id, folder),
        )
        return {r["uid"] for r in cur.fetchall()}

    # ---- Sincronización ----
    def sync_folder(self, folder: str) -> tuple[int, int]:
        typ, _ = self.imap.select(f'"{folder}"')
        if typ != "OK":
            print(f"  ! no se pudo abrir la carpeta {folder}")
            return (0, 0)

        known = self.known_uids(folder)
        total_new = total_att = 0

        if self.fetch_all:
            this_year = datetime.now().year
            for year in range(this_year, 1999, -1):
                criteria = f"(SINCE 01-Jan-{year} BEFORE 01-Jan-{year + 1})"
                n, a = self._run_search(criteria, folder, known)
                total_new += n
                total_att += a
                time.sleep(1.0)
        else:
            since = (datetime.now() - timedelta(days=self.since_days)).strftime("%d-%b-%Y")
            total_new, total_att = self._run_search(f"(SINCE {since})", folder, known)

        cur = self.conn.cursor()
        cur.execute(
            """UPDATE accounts SET
                   last_sync = CURRENT_TIMESTAMP,
                   total_emails = (SELECT COUNT(*) FROM emails WHERE account_id = ?),
                   total_attachments = (SELECT COUNT(*) FROM attachments WHERE account_id = ?)
               WHERE id = ?""",
            (self.account_id, self.account_id, self.account_id),
        )
        self.conn.commit()

        if total_new:
            print(f"  [{folder}] {total_new} correos nuevos, {total_att} adjuntos")
        else:
            print(f"  [{folder}] sin novedades")
        return (total_new, total_att)

    def _run_search(self, criteria: str, folder: str, known: set[int]) -> tuple[int, int]:
        typ, data = self.imap.search(None, criteria)
        if typ != "OK" or not data or not data[0]:
            return (0, 0)

        seq_ids = [x.decode("ascii") for x in data[0].split()]
        new_seqs = self._filter_new(seq_ids, known)
        if not new_seqs:
            return (0, 0)
        return self._download(new_seqs, folder, known)

    def _filter_new(self, seq_ids: list[str], known: set[int]) -> list[str]:
        """Traduce números de secuencia a UID y devuelve solo los desconocidos."""
        uid_map: dict[int, int] = {}
        for i in range(0, len(seq_ids), CHUNK_SIZE):
            chunk = ",".join(seq_ids[i:i + CHUNK_SIZE])
            typ, data = self.imap.fetch(chunk, "(UID)")
            if typ != "OK":
                time.sleep(1)
                continue
            for item in data:
                raw = item[0] if isinstance(item, tuple) else item
                if not isinstance(raw, bytes):
                    continue
                m = re.search(rb"(\d+)\s+\(.*?UID (\d+)", raw)
                if m:
                    uid_map[int(m.group(2))] = int(m.group(1))
            time.sleep(PAUSE_BETWEEN_CHUNKS)

        return [str(seq) for uid, seq in uid_map.items() if uid not in known]

    def _download(self, seqs: list[str], folder: str, known: set[int]) -> tuple[int, int]:
        n_new = n_att = 0
        for i in range(0, len(seqs), CHUNK_SIZE):
            chunk = ",".join(seqs[i:i + CHUNK_SIZE])
            typ, data = self.imap.fetch(chunk, "(RFC822 FLAGS UID)")
            if typ != "OK":
                print("    ! fallo al descargar lote, se reintenta en la próxima corrida")
                time.sleep(2)
                continue

            for j in range(0, len(data), 2):
                if j + 1 >= len(data) or not isinstance(data[j], tuple):
                    continue
                raw_bytes = data[j][1]
                meta = data[j][0].decode("ascii", "replace") if isinstance(data[j][0], bytes) else ""

                uid_m = re.search(r"UID (\d+)", meta)
                if not uid_m:
                    continue
                uid = int(uid_m.group(1))
                if uid in known:
                    continue

                flags_m = re.search(r"FLAGS \(([^)]*)\)", meta)
                flags = flags_m.group(1) if flags_m else ""

                msg = email.message_from_bytes(raw_bytes)
                added, atts = self._store(msg, uid, folder, flags, raw_bytes)
                if added:
                    n_new += 1
                    n_att += atts

            time.sleep(PAUSE_BETWEEN_CHUNKS)
        return (n_new, n_att)

    def _store(self, msg, uid: int, folder: str, flags: str, raw_bytes: bytes) -> tuple[bool, int]:
        name, addr = split_address(msg.get("From"))
        body_plain, body_html = extract_body(msg)

        eml_name = None
        if self.save_eml:
            eml_name = f"{uid}.eml"
            (self.raw_dir / eml_name).write_bytes(raw_bytes)

        cur = self.conn.cursor()
        try:
            cur.execute(
                """INSERT INTO emails
                   (account_id, uid, folder, message_id, sender, sender_name, sender_email,
                    recipients, cc, reply_to, subject, date, body_plain, body_html,
                    raw_size, flags, eml_filename)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.account_id, uid, folder,
                    (msg.get("Message-ID") or "").strip(),
                    decode_mime(msg.get("From")), name, addr,
                    decode_mime(msg.get("To")), decode_mime(msg.get("Cc")),
                    decode_mime(msg.get("Reply-To")),
                    decode_mime(msg.get("Subject")),
                    parse_date(msg.get("Date")),
                    body_plain, body_html,
                    len(raw_bytes), flags, eml_name,
                ),
            )
        except sqlite3.IntegrityError:
            return (False, 0)   # ya existía (uid + folder)

        email_id = cur.lastrowid

        saved = self._save_attachments(msg, email_id, uid)
        cur.execute("UPDATE emails SET attachment_count = ? WHERE id = ?", (saved, email_id))
        self.conn.commit()
        return (True, saved)

    def _save_attachments(self, msg, email_id: int, uid: int) -> int:
        if not msg.is_multipart():
            return 0
        saved = 0
        cur = self.conn.cursor()
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" not in disp:
                continue
            filename = part.get_filename()
            if not filename:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            filename = safe_filename(decode_mime(filename))
            digest = hashlib.md5(payload).hexdigest()[:10]
            stem, ext = os.path.splitext(filename)
            unique = safe_filename(f"{uid}_{stem[:90]}_{digest}{ext}")
            path = self.attach_dir / unique
            path.write_bytes(payload)

            cur.execute(
                """INSERT INTO attachments
                   (email_id, account_id, email_uid, filename, content_type,
                    content_id, size, filepath)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (email_id, self.account_id, uid, filename,
                 part.get_content_type(), part.get("Content-ID", ""),
                 len(payload), str(path)),
            )
            saved += 1
        return saved


# ───────────────────────────── Helpers ─────────────────────────────────

def sanitize_account_dir(addr: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", addr)


def resolve_password(account: dict) -> str:
    """Lee la contraseña del entorno. Nunca se escribe en disco."""
    env_name = account.get("password_env")
    if not env_name:
        raise SystemExit(
            f"La cuenta {account.get('email')} no define 'password_env'. "
            "Por seguridad las contraseñas no se aceptan en el JSON."
        )
    value = os.environ.get(env_name)
    if not value:
        raise SystemExit(
            f"Falta la variable de entorno {env_name} para {account.get('email')}."
        )
    return value


def extract_body(msg) -> tuple[str | None, str | None]:
    """Devuelve (texto_plano, html) del primer bloque de cada tipo."""
    plain = html = None
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = text
            elif ctype == "text/html" and html is None:
                html = text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html = text
            else:
                plain = text

    return (plain.strip() if plain else None, html.strip() if html else None)


def load_accounts(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    return data


def print_status(conn: sqlite3.Connection):
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT a.email, a.label, a.last_sync, a.total_emails, a.total_attachments
        FROM accounts a ORDER BY a.id
    """).fetchall()
    if not rows:
        print("Sin cuentas sincronizadas todavía.")
        return
    print(f"{'CUENTA':<34} {'CORREOS':>8} {'ADJUNTOS':>9}  ÚLTIMA SINCRONIZACIÓN")
    print("-" * 82)
    for r in rows:
        print(f"{r['email']:<34} {r['total_emails']:>8} {r['total_attachments']:>9}  {r['last_sync'] or '—'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sincroniza buzones IMAP a SQLite.")
    ap.add_argument("--config", required=True, help="JSON con las cuentas a sincronizar")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="Ruta de la base SQLite")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                    help="Carpeta donde se guardan adjuntos y .eml")
    ap.add_argument("--days", type=int, default=30, help="Días hacia atrás (default 30)")
    ap.add_argument("--all", action="store_true", help="Descargar todo el histórico")
    ap.add_argument("--save-eml", action="store_true", help="Guardar también el .eml original")
    ap.add_argument("--status", action="store_true", help="Solo mostrar estado y salir")
    args = ap.parse_args()

    db_path = Path(args.db)
    data_dir = Path(args.data_dir)
    conn = connect(db_path)

    if args.status:
        print_status(conn)
        return 0

    accounts = load_accounts(Path(args.config))
    if not accounts:
        print("El config no tiene cuentas.", file=sys.stderr)
        return 1

    db_size_before = db_path.stat().st_size if db_path.exists() else 0
    total_new = 0

    for acc in accounts:
        print(f"\n>> {acc['email']}")
        sync = MailboxSync(acc, conn, data_dir, args.days, args.all, args.save_eml)
        try:
            sync.connect()
        except imaplib.IMAP4.error as exc:
            print(f"  x autenticacion fallida: {exc}", file=sys.stderr)
            continue
        try:
            sync.ensure_account()
            for folder in sync.folders:
                n, _ = sync.sync_folder(folder)
                total_new += n
        finally:
            sync.disconnect()

    db_size_after = db_path.stat().st_size if db_path.exists() else 0
    print(f"\nTotal: {total_new} correos nuevos · base {db_size_before/1048576:.1f} → {db_size_after/1048576:.1f} MB")
    print_status(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
