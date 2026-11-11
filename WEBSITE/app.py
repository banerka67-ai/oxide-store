import os, sqlite3
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / '.env')
DB_PATH = Path(os.getenv('DB_PATH', 'data/oxide.db'))
if not DB_PATH.is_absolute(): DB_PATH = (HERE / DB_PATH).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-only-change-me')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change-me')

def db():
    con=sqlite3.connect(DB_PATH); con.row_factory=sqlite3.Row; return con

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,price REAL NOT NULL,description TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,customer TEXT NOT NULL,contact TEXT NOT NULL,product_id INTEGER NOT NULL,quantity INTEGER NOT NULL DEFAULT 1,total REAL NOT NULL,status TEXT NOT NULL DEFAULT 'Новый',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS cart_sessions(sid TEXT NOT NULL,product_id INTEGER NOT NULL,quantity INTEGER NOT NULL,PRIMARY KEY(sid,product_id))""")

def admin_required(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        if not session.get('admin'): return redirect(url_for('admin_login'))
        return fn(*a,**kw)
    return wrapped

@app.route('/')
def index():
    with db() as c: products=c.execute('SELECT * FROM products WHERE active=1 ORDER BY id DESC').fetchall()
    return render_template('index.html',products=products)

@app.route('/miniapp')
def miniapp():
    with db() as c:
        products=c.execute('SELECT * FROM products WHERE active=1 ORDER BY id DESC').fetchall()
    return render_template('miniapp.html', products=products)

@app.route('/miniapp/admin')
@admin_required
def miniapp_admin():
    with db() as c:
        products=c.execute('SELECT * FROM products ORDER BY id DESC').fetchall()
        orders=c.execute("""SELECT o.*,p.name product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id ORDER BY o.id DESC""").fetchall()
    return render_template('miniapp_admin.html', products=products, orders=orders)

@app.route('/order/<int:pid>', methods=['POST'])
def order(pid):
    customer=request.form.get('customer','').strip(); contact=request.form.get('contact','').strip()
    if not customer or not contact: flash('Заполните имя и контакт.'); return redirect(url_for('index'))
    with db() as c:
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
        if not p: abort(404)
        cur=c.execute('INSERT INTO orders(customer,contact,product_id,quantity,total) VALUES(?,?,?,?,?)',(customer,contact,pid,1,p['price']))
        oid=cur.lastrowid
    flash(f'Заявка №{oid} создана. Администратор свяжется с вами для подтверждения.')
    return redirect(url_for('index'))

@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if request.method=='POST':
        if request.form.get('password','') == ADMIN_PASSWORD and ADMIN_PASSWORD != 'change-me':
            session['admin']=True; return redirect(url_for('admin'))
        flash('Неверный пароль. Проверьте ADMIN_PASSWORD в .env.')
    return render_template('login.html')

@app.route('/admin/logout')
def admin_logout(): session.clear(); return redirect(url_for('index'))

@app.route('/admin')
@admin_required
def admin():
    with db() as c:
        products=c.execute('SELECT * FROM products ORDER BY id DESC').fetchall()
        orders=c.execute("""SELECT o.*,p.name product_name FROM orders o LEFT JOIN products p ON p.id=o.product_id ORDER BY o.id DESC""").fetchall()
    return render_template('admin.html',products=products,orders=orders)

@app.route('/admin/product',methods=['POST'])
@admin_required
def add_product():
    name=request.form.get('name','').strip(); desc=request.form.get('description','').strip()
    try: price=float(request.form.get('price','0').replace(',','.'))
    except ValueError: price=0
    if not name or price<=0: flash('Укажите название и цену больше нуля.')
    else:
        with db() as c: c.execute('INSERT INTO products(name,price,description) VALUES(?,?,?)',(name,price,desc))
        flash('Товар добавлен.')
    return redirect(url_for('admin'))

@app.route('/admin/product/<int:pid>/toggle',methods=['POST'])
@admin_required
def toggle_product(pid):
    with db() as c: c.execute('UPDATE products SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(pid,))
    return redirect(url_for('admin'))

@app.route('/admin/order/<int:oid>/status',methods=['POST'])
@admin_required
def update_order(oid):
    status=request.form.get('status','Новый')
    if status not in ['Новый','В обработке','Выполнен','Отменён']: abort(400)
    with db() as c: c.execute('UPDATE orders SET status=? WHERE id=?',(status,oid))
    return redirect(url_for('admin'))

init_db()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT', '5000')),debug=False)
