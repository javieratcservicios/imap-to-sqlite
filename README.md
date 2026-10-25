# imap-to-sqlite

**Respaldo local y búsqueda SQL de tu correo.** Sincroniza uno o varios buzones IMAP a una base SQLite, con cuerpo, cabeceras y adjuntos en disco. Sin dependencias externas, sin servicios, sin suscripciones.

**Local email backup and SQL search for IMAP mailboxes.** Syncs one or many IMAP accounts into SQLite — bodies, headers and attachments on disk. Standard library only.

---

## Por qué existe

`offlineimap` e `imap-backup` respaldan correo, pero te dejan archivos `.eml` y `maildir`: hay que usar `grep` para encontrar algo. Yo quería **preguntar cosas**:

```sql
SELECT date, sender_email, subject
FROM emails
WHERE body_plain LIKE '%remesa%'
  AND date >= '2026-01-01'
ORDER BY date DESC;
```

Eso es este proyecto. Un archivo `.db` que puedes abrir con cualquier cliente SQLite, copiar a otro lado, o consultar desde Python en dos líneas.

## Características

- **Incremental por UID** — re-ejecutable sin duplicar. La clave única es `(cuenta, uid, carpeta)`.
- **Credenciales solo por entorno** — el JSON de cuentas nunca contiene contraseñas.
- **Búsqueda de texto completo** con FTS5 sobre asunto y cuerpo.
- **Adjuntos deduplicados** por hash MD5 del contenido.
- **Modo histórico completo** (`--all`) recorriendo año por año para no chocar con los límites de Gmail.
- **Multi-cuenta y multi-carpeta** desde un solo archivo de configuración.
- **Cero dependencias** — `imaplib`, `sqlite3`, `email` de la biblioteca estándar.

## Instalación

```bash
git clone https://github.com/TU_USUARIO/imap-to-sqlite.git
cd imap-to-sqlite
# No hay requirements: Python 3.10+ es todo lo que necesitas.
```

## Configuración

Copia el ejemplo y edítalo:

```bash
cp accounts.example.json accounts.json
```

```json
[
  {
    "email": "yo@dominio.com",
    "password_env": "IMAP_PASS_TRABAJO",
    "imap_server": "imap.gmail.com",
    "imap_port": 993,
    "folders": ["INBOX"],
    "label": "Trabajo"
  }
]
```

La contraseña va en una variable de entorno, nunca en el JSON:

```bash
export IMAP_PASS_TRABAJO="abcd efgh ijkl mnop"
```

> **Gmail / Google Workspace:** no uses tu contraseña normal. Genera una **contraseña de aplicación** en <https://myaccount.google.com/apppasswords> y activa la verificación en dos pasos primero.

## Uso

```bash
# Estado de las cuentas ya sincronizadas
python imap_to_sqlite.py --config accounts.json --status

# Últimos 30 días (por defecto)
python imap_to_sqlite.py --config accounts.json --days 30

# Histórico completo, guardando también el .eml original
python imap_to_sqlite.py --config accounts.json --all --save-eml

# Otros buzones que no son Gmail
python imap_to_sqlite.py --config accounts.json --all \
  --db /ruta/mail.db --data-dir /ruta/datos
```

### Opciones

| Opción | Descripción |
|---|---|
| `--config` | JSON con las cuentas (requerido) |
| `--db` | Ruta de la base (default `~/mailbox/mail.db`) |
| `--data-dir` | Carpeta de adjuntos y `.eml` (default `~/mailbox`) |
| `--days` | Días hacia atrás cuando no se usa `--all` (default 30) |
| `--all` | Descarga todo el histórico, año por año |
| `--save-eml` | Guarda además el mensaje original en `.eml` |
| `--status` | Muestra el estado y sale, sin conectarse |

## Estructura en disco

```
~/mailbox/
├── mail.db                          # base SQLite
└── yo@dominio.com/
    ├── attachments/                 # adjuntos, nombre único por hash
    └── raw/                         # .eml (solo con --save-eml)
```

## Consultas útiles

```sql
-- Correos con adjuntos, del remitente que te interesa
SELECT date, subject, attachment_count
FROM emails
WHERE sender_email LIKE '%proveedor.com' AND attachment_count > 0
ORDER BY date DESC LIMIT 20;

-- Búsqueda de texto completo (FTS5)
SELECT e.date, e.subject
FROM emails_fts f JOIN emails e ON e.id = f.rowid
WHERE emails_fts MATCH 'factura AND vencida'
ORDER BY rank LIMIT 20;

-- Adjuntos por tipo de archivo
SELECT content_type, COUNT(*) n, ROUND(SUM(size)/1048576.0, 1) mb
FROM attachments GROUP BY content_type ORDER BY n DESC;

-- Actividad por mes
SELECT strftime('%Y-%m', date) mes, COUNT(*) correos
FROM emails GROUP BY mes ORDER BY mes DESC;
```

## Notas de operación

- **Gmail es quisquilloso.** El script pausa entre lotes a propósito; no lo quites a menos que sepas lo que haces. Si ves `Too many simultaneous connections`, corre una cuenta a la vez.
- **El histórico completo tarda.** Año por año son varias horas en buzones grandes, pero es reanudable: si se corta, lo vuelves a correr y sigue donde iba.
- **La base crece.** Con cuerpo HTML incluido, cuentas de 10 GB pueden producir 2–4 GB de SQLite. El cuerpo HTML es lo que más pesa; si no lo necesitas, puedes limpiarlo con `UPDATE emails SET body_html = NULL`.
- **WAL está activo** (`journal_mode=WAL`). Si copias la base mientras corre, copia también `mail.db-wal` o detén el script primero.

## Licencia

MIT — ver [LICENSE](LICENSE).

---

## English

Backs up IMAP mailboxes into SQLite so you can query your email with plain SQL. Standard library only, no external dependencies.

- Incremental by UID — safe to re-run, never duplicates.
- Passwords come from environment variables, never from the config file.
- Full-text search over subject and body via FTS5.
- Attachments deduplicated by content hash.
- `--all` walks year by year to stay clear of provider rate limits.

```bash
cp accounts.example.json accounts.json   # then set password_env values
export IMAP_PASS_WORK="abcd efgh ijkl mnop"
python imap_to_sqlite.py --config accounts.json --all
```

Use an **app password** for Gmail, not your account password.
