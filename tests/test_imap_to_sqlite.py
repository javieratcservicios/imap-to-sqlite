"""Pruebas con un servidor IMAP simulado en memoria (sin red)."""
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import imap_to_sqlite as m  # noqa: E402

m.PAUSE_BETWEEN_CHUNKS = 0


def make_msg(subject, body="hola", attach=None):
    msg = EmailMessage()
    msg["From"] = "Ana <ana@ejemplo.mx>"
    msg["To"] = "yo@ejemplo.mx"
    msg["Subject"] = subject
    msg["Date"] = "Fri, 23 Oct 2026 16:40:00 -0600"
    msg["Message-ID"] = f"<{subject}@ejemplo.mx>"
    msg.set_content(body)
    if attach:
        msg.add_attachment(attach, maintype="application", subtype="pdf",
                           filename="factura.pdf")
    return msg.as_bytes()


class FakeIMAP:
    """Solo lo que usa MailboxSync: select/response/uid/logout."""

    def __init__(self, folders):
        # folders: {nombre: {"validity": int, "msgs": {uid: bytes}}}
        self.folders = folders
        self.current = None
        self.seq_calls = 0

    def select(self, name, readonly=False):
        self.current = self.folders[name.strip('"')]
        return "OK", [str(len(self.current["msgs"])).encode()]

    def response(self, code):
        assert code == "UIDVALIDITY"
        return code, [str(self.current["validity"]).encode()]

    def uid(self, cmd, *args):
        msgs = self.current["msgs"]
        if cmd == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(msgs)).encode()]
        if cmd == "FETCH":
            out = []
            for u in (int(x) for x in args[0].split(",")):
                if u in msgs:
                    meta = f"1 (UID {u} FLAGS (\\Seen) BODY[] {{{len(msgs[u])}}}".encode()
                    out += [(meta, msgs[u]), b")"]
            return "OK", out
        raise AssertionError(cmd)

    # Si el código vuelve a usar números de secuencia, la prueba truena.
    def search(self, *a):
        raise AssertionError("usar UID SEARCH, no SEARCH")

    def fetch(self, *a):
        raise AssertionError("usar UID FETCH, no FETCH")

    def logout(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAP_PASS_T", "x")
    conn = m.connect(tmp_path / "mail.db")
    acc = {"email": "yo@ejemplo.mx", "password_env": "IMAP_PASS_T",
           "folders": ["INBOX", "Enviados"]}

    def run(server, save_eml=False):
        s = m.MailboxSync(acc, conn, tmp_path, save_eml=save_eml)
        s.imap = server
        s.ensure_account()
        return sum(s.sync_folder(f)[0] for f in s.folders), s

    return conn, run, tmp_path


def server(validity=1):
    return FakeIMAP({
        "INBOX": {"validity": validity, "msgs": {
            1: make_msg("Factura vencida", attach=b"%PDF-1.4 x"),
            2: make_msg("Saludo")}},
        "Enviados": {"validity": 7, "msgs": {1: make_msg("Respuesta")}},
    })


def test_sync_is_incremental(env):
    conn, run, _ = env
    assert run(server())[0] == 3
    assert run(server())[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1


def test_fts_and_dates(env):
    conn, run, _ = env
    run(server())
    row = conn.execute(
        "SELECT e.date FROM emails_fts f JOIN emails e ON e.id = f.rowid "
        "WHERE emails_fts MATCH 'factura'").fetchone()
    assert row is not None
    assert isinstance(row[0], str) and len(row[0]) == 19  # AAAA-MM-DD HH:MM:SS


def test_eml_not_overwritten_across_folders(env):
    """Regresión: UID 1 en INBOX y en Enviados escribían el mismo raw/1.eml."""
    conn, run, tmp = env
    run(server(), save_eml=True)
    raw = tmp / "yo@ejemplo.mx" / "raw"
    assert b"Factura vencida" in (raw / "INBOX" / "1.eml").read_bytes()
    assert b"Respuesta" in (raw / "Enviados" / "1.eml").read_bytes()


def test_uidvalidity_change_archives_old_rows(env):
    conn, run, _ = env
    run(server(validity=1))
    # El servidor recrea INBOX: mismos UID, otros mensajes.
    srv = server(validity=2)
    srv.folders["INBOX"]["msgs"] = {1: make_msg("Nuevo uno"), 2: make_msg("Nuevo dos")}
    assert run(srv)[0] == 2
    folders = dict(conn.execute(
        "SELECT folder, COUNT(*) FROM emails GROUP BY folder").fetchall())
    assert folders["INBOX"] == 2
    assert folders["INBOX [uidvalidity 1]"] == 2
    subjects = {r[0] for r in conn.execute(
        "SELECT subject FROM emails WHERE folder = 'INBOX'")}
    assert subjects == {"Nuevo uno", "Nuevo dos"}


def test_imap_date_is_locale_independent():
    from datetime import datetime
    assert m.imap_date(datetime(2026, 1, 5)) == "05-Jan-2026"


def test_password_never_from_json(monkeypatch):
    with pytest.raises(SystemExit):
        m.resolve_password({"email": "a@b.c", "password": "x"})
