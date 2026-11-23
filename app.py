import os, secrets, datetime, hmac
from functools import wraps
from flask import Flask, request, jsonify, render_template, Response
import psycopg
from psycopg.rows import dict_row

app=Flask(__name__)
DATABASE_URL=os.environ.get('DATABASE_URL')
ADMIN_USER=os.environ.get('ADMIN_USER')
ADMIN_PASSWORD=os.environ.get('ADMIN_PASSWORD')
if not DATABASE_URL: raise RuntimeError('DATABASE_URL is required (PostgreSQL)')
if not ADMIN_USER or not ADMIN_PASSWORD: raise RuntimeError('ADMIN_USER and ADMIN_PASSWORD required')

def conn(): return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init():
 with conn() as c:
  c.execute('CREATE TABLE IF NOT EXISTS demo_orders (id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL, address TEXT NOT NULL, payment_method TEXT NOT NULL, payment_reference TEXT NOT NULL, ticket1 INTEGER NOT NULL UNIQUE, ticket2 INTEGER NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT \'pending\', created_at TIMESTAMPTZ NOT NULL DEFAULT now())')
init()

def admin_required(f):
 @wraps(f)
 def wrapper(*a,**kw):
  auth=request.authorization
  if not auth or not (hmac.compare_digest(auth.username or '',ADMIN_USER) and hmac.compare_digest(auth.password or '',ADMIN_PASSWORD)):
   return Response('Acceso restringido',401,{'WWW-Authenticate':'Basic realm="Wascar Demo Admin"'})
  return f(*a,**kw)
 return wrapper

@app.after_request
def headers(resp):
 resp.headers['Cache-Control']='no-store'
 resp.headers['X-Content-Type-Options']='nosniff'
 resp.headers['X-Frame-Options']='DENY'
 resp.headers['Referrer-Policy']='no-referrer'
 return resp

@app.get('/')
def index(): return render_template('index.html')

@app.get('/admin')
@admin_required
def admin(): return render_template('admin.html')

@app.get('/health')
def health(): return {'ok':True,'mode':'demo'}

@app.post('/api/orders')
def create():
 data=request.get_json(silent=True) or {}
 name=str(data.get('name','')).strip()[:120];phone=str(data.get('phone','')).strip()[:40];address=str(data.get('address','')).strip()[:200];ref=str(data.get('reference','')).strip()[:100];method=data.get('method')
 if len(name)<3 or len(phone)<10 or len(address)<4 or len(ref)<4 or method not in ('transferencia','deposito') or data.get('adult') is not True:
  return jsonify(error='Completa todos los campos y confirma mayoría de edad.'),400
 with conn() as c:
  with c.transaction():
   c.execute('SELECT pg_advisory_xact_lock(20261230)')
   used={r['ticket'] for r in c.execute('SELECT ticket1 AS ticket FROM demo_orders UNION SELECT ticket2 AS ticket FROM demo_orders')}
   available=[i for i in range(1,10001) if i not in used]
   if len(available)<2:return jsonify(error='No quedan números de prueba.'),409
   a,b=secrets.SystemRandom().sample(available,2)
   ident='DEMO-'+secrets.token_hex(5).upper()
   c.execute('INSERT INTO demo_orders(id,name,phone,address,payment_method,payment_reference,ticket1,ticket2,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',(ident,name,phone,address,method,ref,a,b,'pending'))
 return jsonify(id=ident,tickets=[f'{a:05d}',f'{b:05d}'],status='pending',demo=True),201

@app.get('/api/orders')
@admin_required
def orders():
 with conn() as c: rows=c.execute('SELECT * FROM demo_orders ORDER BY created_at DESC LIMIT 500').fetchall()
 for r in rows:r['created_at']=r['created_at'].isoformat()
 return jsonify(orders=rows,demo=True)

@app.get('/api/stats')
@admin_required
def stats():
 with conn() as c:
  row=c.execute("SELECT COUNT(*) AS orders, COUNT(*) FILTER (WHERE status='approved') AS approved, COUNT(*) FILTER (WHERE status='pending') AS pending FROM demo_orders").fetchone()
 return jsonify(**row,demo=True)

@app.post('/api/status')
@admin_required
def status():
 data=request.get_json(silent=True) or {}
 if data.get('status') not in ('pending','approved','rejected'):return jsonify(error='Estado inválido'),400
 with conn() as c:
  r=c.execute('UPDATE demo_orders SET status=%s WHERE id=%s RETURNING id',(data['status'],str(data.get('id','')))).fetchone()
 return (jsonify(ok=True,demo=True),200) if r else (jsonify(error='No encontrado'),404)
