import os, secrets, hmac, re, io
from functools import wraps
from flask import Flask, request, jsonify, render_template, Response, send_file
import psycopg
from psycopg.rows import dict_row

app = Flask(__name__)
DATABASE_URL = os.environ.get('DATABASE_URL')
ADMIN_USER = os.environ.get('ADMIN_USER')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
if not DATABASE_URL: raise RuntimeError('DATABASE_URL is required (PostgreSQL)')
if not ADMIN_USER or not ADMIN_PASSWORD: raise RuntimeError('ADMIN_USER and ADMIN_PASSWORD required')

PRICE_PER_TICKET = 1000
MAX_TICKETS_PER_ORDER = 100
MAX_RECEIPT_BYTES = 5 * 1024 * 1024
app.config['MAX_CONTENT_LENGTH'] = MAX_RECEIPT_BYTES + 128 * 1024

def conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init():
    with conn() as c:
        # Preserve existing demonstration orders and their original schema.
        c.execute("""CREATE TABLE IF NOT EXISTS demo_orders (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
            address TEXT NOT NULL, payment_method TEXT NOT NULL,
            payment_reference TEXT NOT NULL, ticket1 INTEGER NOT NULL UNIQUE,
            ticket2 INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        c.execute("""CREATE TABLE IF NOT EXISTS purchase_requests (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            address TEXT NOT NULL,
            payment_method TEXT NOT NULL,
            payment_reference TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 100),
            unit_price INTEGER NOT NULL CHECK (unit_price = 1000),
            total INTEGER NOT NULL,
            receipt_data BYTEA NOT NULL,
            receipt_mime TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','rejected','delivered')),
            printed_ticket_numbers TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        c.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS bank TEXT")

init()

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or not (hmac.compare_digest(auth.username or '', ADMIN_USER) and hmac.compare_digest(auth.password or '', ADMIN_PASSWORD)):
            return Response('Acceso restringido', 401, {'WWW-Authenticate': 'Basic realm="Wascar Admin"'})
        return f(*args, **kwargs)
    return wrapper

@app.after_request
def headers(resp):
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    return resp

@app.get('/')
def index(): return render_template('index.html')

@app.get('/admin')
@admin_required
def admin(): return render_template('admin.html')

@app.get('/health')
def health(): return {'ok': True, 'mode': 'demo'}

# Preserve the old demonstration form until index.html is updated.
@app.post('/api/orders')
def create_legacy():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()[:120]
    phone = str(data.get('phone', '')).strip()[:40]
    address = str(data.get('address', '')).strip()[:200]
    ref = str(data.get('reference', '')).strip()[:100]
    method = data.get('method')
    if len(name) < 3 or len(phone) < 10 or len(address) < 4 or len(ref) < 4 or method not in ('transferencia', 'deposito') or data.get('adult') is not True:
        return jsonify(error='Completa todos los campos y confirma mayoría de edad.'), 400
    with conn() as c:
        with c.transaction():
            c.execute('SELECT pg_advisory_xact_lock(20261230)')
            used = {r['ticket'] for r in c.execute('SELECT ticket1 AS ticket FROM demo_orders UNION SELECT ticket2 AS ticket FROM demo_orders')}
            available = [i for i in range(1, 10001) if i not in used]
            if len(available) < 2:
                return jsonify(error='No quedan números de prueba.'), 409
            a, b = secrets.SystemRandom().sample(available, 2)
            ident = 'DEMO-' + secrets.token_hex(5).upper()
            c.execute('INSERT INTO demo_orders(id,name,phone,address,payment_method,payment_reference,ticket1,ticket2,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)', (ident,name,phone,address,method,ref,a,b,'pending'))
    return jsonify(id=ident,tickets=[f'{a:05d}',f'{b:05d}'],status='pending',demo=True), 201

@app.get('/api/orders')
@admin_required
def legacy_orders():
    with conn() as c:
        rows = c.execute('SELECT * FROM demo_orders ORDER BY created_at DESC LIMIT 500').fetchall()
    for r in rows: r['created_at'] = r['created_at'].isoformat()
    return jsonify(orders=rows, demo=True)

@app.get('/api/stats')
@admin_required
def stats():
    with conn() as c:
        row = c.execute("SELECT COUNT(*) AS orders, COUNT(*) FILTER (WHERE status='approved') AS approved, COUNT(*) FILTER (WHERE status='pending') AS pending FROM demo_orders").fetchone()
    return jsonify(**row, demo=True)

@app.post('/api/status')
@admin_required
def legacy_status():
    data = request.get_json(silent=True) or {}
    if data.get('status') not in ('pending', 'approved', 'rejected'):
        return jsonify(error='Estado inválido'), 400
    with conn() as c:
        r = c.execute('UPDATE demo_orders SET status=%s WHERE id=%s RETURNING id', (data['status'], str(data.get('id', '')))).fetchone()
    return (jsonify(ok=True, demo=True), 200) if r else (jsonify(error='No encontrado'), 404)

def detect_image(raw):
    if raw.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if raw.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP': return 'image/webp'
    return None

@app.post('/api/purchase-requests')
def create_purchase_request():
    name = request.form.get('name', '').strip()[:120]
    phone = request.form.get('phone', '').strip()[:40]
    address = request.form.get('address', '').strip()[:200]
    reference = request.form.get('reference', '').strip()[:100]
    method = request.form.get('method', '')
    bank = request.form.get('bank', '')
    if bank not in ('banreservas', 'popular'):
        return jsonify(error='Selecciona el banco al que realizaste el pago.'), 400

    if request.form.get('adult') != 'true':
        return jsonify(error='Debes confirmar mayoría de edad.'), 400
    try:
        quantity = int(request.form.get('quantity', ''))
    except ValueError:
        return jsonify(error='Cantidad inválida.'), 400
    if not (1 <= quantity <= MAX_TICKETS_PER_ORDER):
        return jsonify(error='Cantidad fuera del rango permitido.'), 400
    if len(name) < 3 or len(re.sub(r'\D', '', phone)) < 10 or len(address) < 4 or len(reference) < 4 or method not in ('transferencia', 'deposito'):
        return jsonify(error='Completa los datos del comprador y del pago.'), 400
    file = request.files.get('receipt')
    if not file:
        return jsonify(error='Adjunta la foto del comprobante.'), 400
    raw = file.read(MAX_RECEIPT_BYTES + 1)
    if not raw or len(raw) > MAX_RECEIPT_BYTES:
        return jsonify(error='El comprobante debe pesar entre 1 byte y 5 MB.'), 400
    mime = detect_image(raw)
    if mime is None:
        return jsonify(error='Solo se aceptan imágenes JPG, PNG o WebP.'), 400
    ident = 'SOL-' + secrets.token_hex(8).upper()
    total = quantity * PRICE_PER_TICKET
    with conn() as c:
        c.execute("""INSERT INTO purchase_requests
            (id,name,phone,address,payment_method,payment_reference,quantity,unit_price,total,receipt_data,receipt_mime,bank)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (ident,name,phone,address,method,reference,quantity,PRICE_PER_TICKET,total,raw,mime,bank))
    return jsonify(id=ident,quantity=quantity,unit_price=PRICE_PER_TICKET,total=total,status='pending',demo=False), 201

@app.get('/api/purchase-requests')
@admin_required
def list_purchase_requests():
    query = request.args.get('q', '').strip()[:120]
    try:
        page = max(1, int(request.args.get('page', '1')))
    except ValueError:
        page = 1
    page = min(page, 1000000)
    size = 20
    pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    where = "WHERE name ILIKE %s ESCAPE '\\\\' OR phone ILIKE %s ESCAPE '\\\\' OR id ILIKE %s ESCAPE '\\\\' OR printed_ticket_numbers ILIKE %s ESCAPE '\\\\'"
    params = (pattern, pattern, pattern, pattern)
    with conn() as c:
        total = c.execute('SELECT COUNT(*) AS n FROM purchase_requests ' + (where if query else ''), params if query else ()).fetchone()['n']
        rows = c.execute("""SELECT id,name,phone,address,payment_method,payment_reference,
            quantity,unit_price,total,status,printed_ticket_numbers,bank,created_at,updated_at
            FROM purchase_requests """ + (where if query else '') +
            ' ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s',
            (params if query else ()) + (size, (page - 1) * size)).fetchall()
    for r in rows:
        r['created_at'] = r['created_at'].isoformat()
        r['updated_at'] = r['updated_at'].isoformat()
    return jsonify(orders=rows, total=total, page=page, page_size=size, demo=False)

@app.get('/api/purchase-requests/<order_id>/receipt')
@admin_required
def get_receipt(order_id):
    with conn() as c:
        row = c.execute('SELECT receipt_data,receipt_mime FROM purchase_requests WHERE id=%s', (order_id,)).fetchone()
    if row is None: return jsonify(error='No encontrado'), 404
    return send_file(io.BytesIO(bytes(row['receipt_data'])), mimetype=row['receipt_mime'], as_attachment=False, download_name='comprobante')

@app.post('/api/purchase-requests/<order_id>/status')
@admin_required
def update_purchase_status(order_id):
    data = request.get_json(silent=True) or {}
    new_status = data.get('status')
    if new_status not in ('pending','approved','rejected','delivered'):
        return jsonify(error='Estado inválido'), 400
    # Cada casilla del panel corresponde a un boleto físico.
    supplied = data.get('ticket_numbers')
    if supplied is None:
        legacy = str(data.get('printed_ticket_numbers') or '').strip()
        supplied = [x.strip() for x in re.split(r'[,;\n]', legacy)] if legacy else []
    if not isinstance(supplied, list) or len(supplied) > MAX_TICKETS_PER_ORDER:
        return jsonify(error='Lista de boletos inválida.'), 400
    if any(not isinstance(x, str) for x in supplied):
        return jsonify(error='Los números de boletos deben ser texto.'), 400
    parts = [x.strip() for x in supplied]
    if any(x and (len(x) > 30 or not x.isdecimal() or not x.isascii()) for x in parts):
        return jsonify(error='Cada casilla debe contener solo dígitos (0-9).'), 400
    if any(x and int(x) == 0 for x in parts):
        return jsonify(error='Los números deben ser mayores que cero.'), 400
    if any(not x for x in parts) and any(parts):
        return jsonify(error='Completa todos los espacios de boletos o déjalos todos vacíos.'), 400
    canonical = [str(int(x)) for x in parts if x]
    if len(canonical) != len(set(canonical)):
        return jsonify(error='Hay números de boletos repetidos en esta compra.'), 409
    with conn() as c:
        with c.transaction():
            # Serializa todas las asignaciones, incluso si dos administradores guardan a la vez.
            c.execute('SELECT pg_advisory_xact_lock(20261231)')
            row = c.execute('SELECT status,quantity FROM purchase_requests WHERE id=%s FOR UPDATE', (order_id,)).fetchone()
            if not row:
                return jsonify(error='No encontrado'), 404
            if len(parts) > row['quantity']:
                return jsonify(error='No puedes asignar más boletos que los comprados.'), 400
            if new_status == 'delivered':
                if row['status'] not in ('approved','delivered'):
                    return jsonify(error='Confirma primero el pago.'), 409
                if len(canonical) != row['quantity']:
                    return jsonify(error='Debes registrar todos los boletos antes de marcar Entregado.'), 400
            if canonical and new_status not in ('approved','delivered'):
                return jsonify(error='Aprueba el pago antes de asignar números.'), 409
            others = c.execute(
                "SELECT id,printed_ticket_numbers FROM purchase_requests WHERE id<>%s AND printed_ticket_numbers IS NOT NULL AND printed_ticket_numbers<>''",
                (order_id,)).fetchall()
            wanted = set(canonical)
            for other in others:
                existing = {str(int(x)) for x in re.findall(r'\\d+', other['printed_ticket_numbers'])}
                collision = wanted.intersection(existing)
                if collision:
                    return jsonify(error='El boleto '+sorted(collision, key=int)[0]+' ya está asignado a la solicitud '+other['id']+'.'), 409
            c.execute("""UPDATE purchase_requests
                SET status=%s, printed_ticket_numbers=%s, updated_at=now()
                WHERE id=%s""", (new_status, ', '.join(parts) if canonical else None, order_id))
    return jsonify(ok=True,status=new_status,demo=False)
