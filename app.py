import os, secrets, hmac, re, io
from psycopg import errors
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
SALES_ENABLED = os.environ.get('SALES_ENABLED', '').lower() == 'true'
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
        c.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS is_live BOOLEAN NOT NULL DEFAULT FALSE")
        c.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ")
        c.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ")
        c.execute("""CREATE TABLE IF NOT EXISTS assigned_tickets (
            ticket_number TEXT PRIMARY KEY,
            order_id TEXT NOT NULL REFERENCES purchase_requests(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
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
def index(): return render_template('index.html', sales_enabled=SALES_ENABLED)

@app.get('/admin')
@admin_required
def admin(): return render_template('admin.html')

@app.get('/health')
def health(): return {'ok': True, 'mode': 'production' if SALES_ENABLED else 'paused'}

# Preserve the old demonstration form until index.html is updated.
@app.post('/api/orders')
def create_legacy():
    return jsonify(error='El formulario antiguo está deshabilitado.'), 410
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
    return jsonify(id=ident,tickets=[f'{a:05d}',f'{b:05d}'],status='pending',demo=False), 201

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
    if not SALES_ENABLED:
        return jsonify(error='Las ventas aún no están habilitadas. No realices transferencias.'), 503
    bank = request.form.get('bank', '')
    if bank not in ('banreservas','popular'):
        return jsonify(error='Selecciona la cuenta bancaria de destino.'), 400
    name = request.form.get('name', '').strip()[:120]
    phone = request.form.get('phone', '').strip()[:40]
    address = request.form.get('address', '').strip()[:200]
    reference = request.form.get('reference', '').strip()[:100]
    method = request.form.get('method', '')
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
            (id,name,phone,address,payment_method,payment_reference,quantity,unit_price,total,receipt_data,receipt_mime,bank,is_live)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)""",
            (ident,name,phone,address,method,reference,quantity,PRICE_PER_TICKET,total,raw,mime,bank))
    return jsonify(id=ident,quantity=quantity,unit_price=PRICE_PER_TICKET,total=total,status='pending',demo=False), 201

@app.get('/api/purchase-requests')
@admin_required
def list_purchase_requests():
    with conn() as c:
        rows = c.execute("""SELECT id,name,phone,address,payment_method,payment_reference,
            quantity,unit_price,total,status,printed_ticket_numbers,bank,is_live,approved_at,delivered_at,created_at,updated_at
            FROM purchase_requests ORDER BY created_at DESC LIMIT 500""").fetchall()
    for r in rows:
        r['created_at'] = r['created_at'].isoformat()
        r['updated_at'] = r['updated_at'].isoformat()
        for key in ('approved_at','delivered_at'):
            if r[key]: r[key] = r[key].isoformat()
    return jsonify(orders=rows, demo=False)

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
    raw = str(data.get('printed_ticket_numbers') or '').strip()
    numbers = [v.strip() for v in raw.split(',') if v.strip()]
    if len(numbers) > MAX_TICKETS_PER_ORDER or any(not re.fullmatch(r'[0-9]{1,10}', n) for n in numbers):
        return jsonify(error='Escribe números de boletos separados por comas, solo dígitos.'), 400
    # Canonicalize leading zeros: 00025 and 25 refer to the same physical number.
    canonical = [str(int(n)) for n in numbers]
    if len(set(canonical)) != len(canonical):
        return jsonify(error='Hay números repetidos en esta solicitud.'), 409
    try:
        with conn() as c:
            with c.transaction():
                row = c.execute('SELECT * FROM purchase_requests WHERE id=%s FOR UPDATE', (order_id,)).fetchone()
                if row is None: return jsonify(error='Solicitud no encontrada'), 404
                if not row['is_live']:
                    return jsonify(error='Registro de demostración: no puede aprobarse como venta real.'), 409
                if not SALES_ENABLED:
                    return jsonify(error='Ventas pausadas: no se pueden aprobar solicitudes.'), 409
                current = row['status']
                if current == 'delivered' and new_status != 'delivered':
                    return jsonify(error='Una entrega completada no puede cambiar de estado.'), 409
                if current == 'rejected' and new_status != 'rejected':
                    return jsonify(error='Una solicitud rechazada no puede reabrirse.'), 409
                if current == 'approved' and new_status not in ('approved','delivered'):
                    return jsonify(error='Un pago aprobado no puede volver a pendiente o rechazado.'), 409
                if new_status == 'delivered' and current not in ('approved','delivered'):
                    return jsonify(error='Primero aprueba el pago y guarda los números.'), 409
                existing = [r['ticket_number'] for r in c.execute(
                    'SELECT ticket_number FROM assigned_tickets WHERE order_id=%s ORDER BY ticket_number', (order_id,))]
                if new_status in ('approved','delivered'):
                    if not numbers and existing:
                        canonical = existing
                    if len(canonical) != row['quantity']:
                        return jsonify(error=f'Debes registrar exactamente {row["quantity"]} números físicos.'), 400
                    if existing and set(existing) != set(canonical):
                        return jsonify(error='Los números ya asignados no pueden modificarse después de aprobar el pago.'), 409
                    if not existing:
                        for number in canonical:
                            c.execute('INSERT INTO assigned_tickets(ticket_number,order_id) VALUES (%s,%s)', (number,order_id))
                elif canonical:
                    return jsonify(error='Solo puedes asignar números al aprobar el pago.'), 400
                stored = ', '.join(numbers) if numbers else row['printed_ticket_numbers']
                c.execute("""UPDATE purchase_requests SET status=%s,
                    printed_ticket_numbers=%s,
                    approved_at=CASE WHEN %s='approved' AND approved_at IS NULL THEN now() ELSE approved_at END,
                    delivered_at=CASE WHEN %s='delivered' AND delivered_at IS NULL THEN now() ELSE delivered_at END,
                    updated_at=now() WHERE id=%s""",
                    (new_status,stored,new_status,new_status,order_id))
    except errors.UniqueViolation:
        return jsonify(error='Uno o más números físicos ya están asignados a otro comprador.'), 409
    return jsonify(ok=True,status=new_status,demo=False)
