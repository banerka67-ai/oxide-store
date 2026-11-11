import os, sqlite3, json, hashlib, hmac, secrets, urllib.request, urllib.error
from pathlib import Path
from werkzeug.utils import secure_filename
import uuid
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
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
PUBLIC_URL = os.getenv('PUBLIC_URL', 'https://oxide-store.onrender.com').rstrip('/')
WEBHOOK_SECRET = hashlib.sha256(app.secret_key.encode()).hexdigest()
UPLOAD_DIR = HERE / 'static' / 'uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
MAX_IMAGE_SIZE = 5 * 1024 * 1024


def save_product_image(file):
    if not file or not file.filename:
        return ''
    original = secure_filename(file.filename)
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError('Формат фото: PNG, JPG, WEBP или GIF.')
    data = file.read(MAX_IMAGE_SIZE + 1)
    if not data or len(data) > MAX_IMAGE_SIZE:
        raise ValueError('Фото должно быть не больше 5 МБ.')
    if not file.mimetype.startswith('image/'):
        raise ValueError('Можно загружать только изображения.')
    filename = f'{uuid.uuid4().hex}{ext}'
    (UPLOAD_DIR / filename).write_bytes(data)
    return f'/static/uploads/{filename}'

def telegram_api(method, payload):
    if not BOT_TOKEN:
        return None
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{BOT_TOKEN}/{method}',
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode('utf-8'))

def configure_telegram_webhook():
    if not BOT_TOKEN:
        app.logger.warning('BOT_TOKEN is not configured; Telegram /start is disabled.')
        return
    try:
        result = telegram_api('setWebhook', {
            'url': f'{PUBLIC_URL}/telegram/webhook',
            'secret_token': WEBHOOK_SECRET,
            'allowed_updates': ['message', 'pre_checkout_query']
        })
        if result and result.get('ok'):
            app.logger.info('Telegram webhook configured.')
        else:
            app.logger.error('Telegram webhook setup failed.')
    except Exception:
        app.logger.exception('Could not configure Telegram webhook.')

def db():
    con=sqlite3.connect(DB_PATH); con.row_factory=sqlite3.Row; return con

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,price REAL NOT NULL,description TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,customer TEXT NOT NULL,contact TEXT NOT NULL,product_id INTEGER NOT NULL,quantity INTEGER NOT NULL DEFAULT 1,total REAL NOT NULL,status TEXT NOT NULL DEFAULT 'Новый',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS cart_sessions(sid TEXT NOT NULL,product_id INTEGER NOT NULL,quantity INTEGER NOT NULL,PRIMARY KEY(sid,product_id))""")
        cols = [r['name'] for r in c.execute('PRAGMA table_info(products)').fetchall()]
        if 'stars_price' not in cols:
            c.execute('ALTER TABLE products ADD COLUMN stars_price INTEGER NOT NULL DEFAULT 0')
        if 'image_url' not in cols:
            c.execute("ALTER TABLE products ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS star_payments(payload TEXT PRIMARY KEY, order_ids TEXT NOT NULL, total_stars INTEGER NOT NULL, telegram_user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS product_keys(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, key_text TEXT NOT NULL, claimed_order_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(product_id,key_text))""")

def admin_required(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        if not session.get('admin'): return redirect(url_for('admin_login'))
        return fn(*a,**kw)
    return wrapped

@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    if request.headers.get('X-Telegram-Bot-Api-Secret-Token', '') != WEBHOOK_SECRET:
        abort(403)
    update = request.get_json(silent=True) or {}
    pre = update.get('pre_checkout_query')
    if pre:
        payload = str(pre.get('invoice_payload', ''))
        with db() as c:
            payment = c.execute('SELECT total_stars,status,telegram_user_id FROM star_payments WHERE payload=?', (payload,)).fetchone()
        valid = bool(payment and payment['status'] == 'pending' and pre.get('currency') == 'XTR' and int(pre.get('total_amount', 0)) == int(payment['total_stars']) and int((pre.get('from') or {}).get('id', 0)) == int(payment['telegram_user_id']))
        try:
            telegram_api('answerPreCheckoutQuery', {'pre_checkout_query_id': pre.get('id'), 'ok': valid, **({} if valid else {'error_message': 'Счёт недействителен. Создай новый заказ.'})})
        except Exception:
            app.logger.exception('Could not answer Telegram pre-checkout query.')
        return {'ok': True}
    message = update.get('message') or {}
    successful = message.get('successful_payment')
    if successful:
        payload = str(successful.get('invoice_payload', ''))
        with db() as c:
            payment = c.execute('SELECT order_ids,total_stars,status,telegram_user_id FROM star_payments WHERE payload=?', (payload,)).fetchone()
            if payment and payment['status'] == 'pending' and successful.get('currency') == 'XTR' and int(successful.get('total_amount', 0)) == int(payment['total_stars']) and int((message.get('from') or {}).get('id', 0)) == int(payment['telegram_user_id']):
                for oid in json.loads(payment['order_ids']):
                    c.execute("UPDATE orders SET status='Оплачен' WHERE id=?", (int(oid),))
                c.execute("UPDATE star_payments SET status='paid' WHERE payload=?", (payload,))
        return {'ok': True}
    text = (message.get('text') or '').strip()
    chat = message.get('chat') or {}
    chat_id = chat.get('id')
    if chat_id and text.split(' ', 1)[0].split('@', 1)[0] == '/start':
        try:
            telegram_api('sendMessage', {
                'chat_id': chat_id,
                'text': '👑 Добро пожаловать в CrownStore!\n\n🛍 Здесь ты найдёшь цифровые товары по отличным ценам.\n\n⚡ Выбирай товар, оформляй заказ и оплачивай прямо в Telegram!\n\n👇 Нажимай кнопку ниже, чтобы перейти в магазин.',
                'reply_markup': {
                    'inline_keyboard': [[{
                        'text': 'Открыть магазин',
                        'web_app': {'url': f'{PUBLIC_URL}/miniapp'}
                    }]]
                }
            })
        except Exception:
            app.logger.exception('Could not send Telegram welcome message.')
    return {'ok': True}

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
        stock_rows=c.execute('SELECT product_id,COUNT(*) AS stock FROM product_keys WHERE claimed_order_id IS NULL GROUP BY product_id').fetchall()
        stock={row['product_id']:row['stock'] for row in stock_rows}
    return render_template('miniapp_admin.html', products=products, orders=orders, stock=stock)


@app.route('/api/order', methods=['POST'])
def create_miniapp_order():
    data = request.get_json(silent=True) or {}
    customer = str(data.get('customer', '')).strip()[:80]
    contact = str(data.get('contact', '')).strip()[:120]
    items = data.get('items')
    if not customer or not contact:
        return {'ok': False, 'error': 'Укажи имя и контакт.'}, 400
    if not isinstance(items, list) or not items or len(items) > 30:
        return {'ok': False, 'error': 'Корзина пуста или содержит слишком много позиций.'}, 400
    clean = {}
    for item in items:
        if not isinstance(item, dict):
            return {'ok': False, 'error': 'Некорректная корзина.'}, 400
        try:
            pid, qty = int(item.get('id')), int(item.get('qty'))
        except (TypeError, ValueError):
            return {'ok': False, 'error': 'Некорректное количество товара.'}, 400
        if pid <= 0 or qty < 1 or qty > 99:
            return {'ok': False, 'error': 'Количество должно быть от 1 до 99.'}, 400
        clean[pid] = min(99, clean.get(pid, 0) + qty)
    order_ids, total = [], 0.0
    with db() as c:
        for pid, qty in clean.items():
            product = c.execute('SELECT id, price FROM products WHERE id=? AND active=1', (pid,)).fetchone()
            if not product:
                return {'ok': False, 'error': 'Один из товаров больше недоступен. Обнови каталог.'}, 400
            amount = round(float(product['price']) * qty, 2)
            cur = c.execute('INSERT INTO orders(customer,contact,product_id,quantity,total) VALUES(?,?,?,?,?)',
                            (customer, contact, pid, qty, amount))
            order_ids.append(cur.lastrowid)
            total += amount
    return {'ok': True, 'order_ids': order_ids, 'total': round(total, 2),
            'message': 'Заявка отправлена! Администратор свяжется с тобой для подтверждения.'}


def verify_telegram_init_data(init_data):
    if not BOT_TOKEN or not init_data:
        return None
    try:
        from urllib.parse import parse_qsl
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop('hash', '')
        if not received_hash:
            return None
        check_string = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
        secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        import time
        if int(pairs.get('auth_date', '0')) < int(time.time()) - 86400:
            return None
        user = json.loads(pairs.get('user', '{}'))
        return int(user.get('id'))
    except Exception:
        return None

@app.route('/api/create-invoice', methods=['POST'])
def create_star_invoice():
    telegram_user_id = verify_telegram_init_data(request.headers.get('X-Telegram-Init-Data', ''))
    if not telegram_user_id:
        return {'ok': False, 'error': 'Открой магазин через Telegram-бота, чтобы оплатить Stars.'}, 401
    data = request.get_json(silent=True) or {}
    customer = str(data.get('customer', '')).strip()[:80]
    contact = str(data.get('contact', '')).strip()[:120]
    items = data.get('items')
    if not customer or not contact or not isinstance(items, list) or not items or len(items) > 30:
        return {'ok': False, 'error': 'Проверь имя, контакт и корзину.'}, 400
    clean = {}
    for item in items:
        try:
            pid, qty = int(item.get('id')), int(item.get('qty'))
        except (TypeError, ValueError, AttributeError):
            return {'ok': False, 'error': 'Некорректная корзина.'}, 400
        if pid <= 0 or qty < 1 or qty > 99:
            return {'ok': False, 'error': 'Некорректное количество.'}, 400
        clean[pid] = min(99, clean.get(pid, 0) + qty)
    payload = secrets.token_urlsafe(24)
    order_ids, total_stars, title_parts = [], 0, []
    with db() as c:
        for pid, qty in clean.items():
            product = c.execute('SELECT id,name,stars_price FROM products WHERE id=? AND active=1', (pid,)).fetchone()
            if not product:
                return {'ok': False, 'error': 'Товар больше недоступен. Обнови каталог.'}, 400
            stars = int(product['stars_price'] or 0)
            if stars < 1:
                return {'ok': False, 'error': f'Для товара «{product["name"]}» ещё не задана цена в Stars.'}, 400
            cur = c.execute("INSERT INTO orders(customer,contact,product_id,quantity,total,status) VALUES(?,?,?,?,?,'Ожидает оплаты')", (customer, contact, pid, qty, float(stars * qty)))
            order_ids.append(cur.lastrowid)
            total_stars += stars * qty
            title_parts.append(product['name'])
        if total_stars < 1 or total_stars > 1000000:
            return {'ok': False, 'error': 'Некорректная сумма оплаты.'}, 400
        c.execute('INSERT INTO star_payments(payload,order_ids,total_stars,telegram_user_id) VALUES(?,?,?,?)', (payload, json.dumps(order_ids), total_stars, telegram_user_id))
    try:
        invoice = telegram_api('createInvoiceLink', {'title': 'CrownStore заказ', 'description': ', '.join(title_parts)[:255] or 'Цифровые товары CrownStore', 'payload': payload, 'provider_token': '', 'currency': 'XTR', 'prices': [{'label': 'Заказ CrownStore', 'amount': total_stars}]})
        if not invoice or not invoice.get('ok') or not invoice.get('result'):
            raise RuntimeError('Telegram не создал счёт.')
        return {'ok': True, 'invoice_url': invoice['result'], 'total_stars': total_stars}
    except Exception:
        app.logger.exception('Could not create Telegram Stars invoice.')
        with db() as c:
            for oid in order_ids:
                c.execute('DELETE FROM orders WHERE id=? AND status=?', (oid, 'Ожидает оплаты'))
            c.execute('DELETE FROM star_payments WHERE payload=?', (payload,))
        return {'ok': False, 'error': 'Не удалось создать счёт Stars. Проверь BOT_TOKEN.'}, 502

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
    image_url=request.form.get('image_url','').strip()
    try: price=float(request.form.get('price','0').replace(',','.'))
    except ValueError: price=0
    if not name or price<=0:
        flash('Укажите название и цену больше нуля.')
    elif image_url and not image_url.startswith(('https://','http://')):
        flash('Ссылка на картинку должна начинаться с https:// или http://.')
    else:
        try:
            stars_price = max(0, int(request.form.get('stars_price', '0') or 0))
            uploaded = save_product_image(request.files.get('image_file'))
            if uploaded: image_url = uploaded
            with db() as c:
                c.execute('INSERT INTO products(name,price,description,stars_price,image_url) VALUES(?,?,?,?,?)',(name,price,desc,stars_price,image_url))
            flash('Товар добавлен.')
        except ValueError as exc:
            flash(str(exc))
    return redirect(url_for('miniapp_admin'))


@app.route('/admin/product/<int:pid>/image', methods=['POST'])
@admin_required
def update_product_image(pid):
    image_url = request.form.get('image_url', '').strip()
    if image_url and not image_url.startswith(('https://', 'http://')):
        flash('Ссылка должна начинаться с https:// или http://.')
    else:
        with db() as c:
            c.execute('UPDATE products SET image_url=? WHERE id=?', (image_url, pid))
        flash('Картинка товара обновлена.' if image_url else 'Картинка товара удалена.')
    return redirect(url_for('miniapp_admin'))


@app.route('/admin/product/<int:pid>/stars', methods=['POST'])
@admin_required
def update_product_stars(pid):
    try:
        stars_price = int(request.form.get('stars_price', '0'))
    except ValueError:
        stars_price = 0
    if stars_price < 1 or stars_price > 1000000:
        flash('Цена в Stars должна быть целым числом от 1 до 1 000 000.')
    else:
        with db() as c:
            c.execute('UPDATE products SET stars_price=? WHERE id=?', (stars_price, pid))
        flash('Цена в Stars обновлена.')
    return redirect(url_for('miniapp_admin'))

@app.route('/admin/product/<int:pid>/keys', methods=['POST'])
@admin_required
def add_product_keys(pid):
    raw = request.form.get('keys', '')
    keys = list(dict.fromkeys(line.strip() for line in raw.splitlines() if line.strip()))
    if not keys:
        flash('Вставь ключи: каждый с новой строки.')
    elif len(keys) > 1000 or any(len(k) > 500 for k in keys):
        flash('Слишком много ключей или слишком длинная строка.')
    else:
        with db() as c:
            if not c.execute('SELECT id FROM products WHERE id=?', (pid,)).fetchone(): abort(404)
            before = c.total_changes
            c.executemany('INSERT OR IGNORE INTO product_keys(product_id,key_text) VALUES(?,?)', [(pid,k) for k in keys])
            added = c.total_changes - before
        flash(f'Добавлено ключей: {added}. Дубликаты пропущены.')
    return redirect(url_for('miniapp_admin'))

@app.route('/admin/order/<int:oid>/deliver', methods=['POST'])
@admin_required
def deliver_order(oid):
    with db() as c:
        order = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        if not order: abort(404)
        if order['status'] not in ('Оплачен', 'Выполнен'):
            flash('Сначала дождись подтверждения оплаты Stars.')
            return redirect(url_for('miniapp_admin'))
        payments = c.execute("SELECT order_ids,telegram_user_id FROM star_payments WHERE status='paid'").fetchall()
        user_id = next((int(p['telegram_user_id']) for p in payments if oid in [int(x) for x in json.loads(p['order_ids'])]), None)
        if not user_id:
            flash('Не найдено подтверждение оплаты Stars для этого заказа.')
            return redirect(url_for('miniapp_admin'))
        assigned = c.execute('SELECT id,key_text FROM product_keys WHERE claimed_order_id=? ORDER BY id', (oid,)).fetchall()
        if not assigned:
            available = c.execute('SELECT id,key_text FROM product_keys WHERE product_id=? AND claimed_order_id IS NULL ORDER BY id LIMIT ?', (order['product_id'], int(order['quantity']))).fetchall()
            if len(available) < int(order['quantity']):
                flash(f'Недостаточно ключей. Нужно: {order["quantity"]}, доступно: {len(available)}.')
                return redirect(url_for('miniapp_admin'))
            for key in available: c.execute('UPDATE product_keys SET claimed_order_id=? WHERE id=? AND claimed_order_id IS NULL', (oid, key['id']))
            assigned = available
    keys = [x['key_text'] for x in assigned]
    msg = '👑 CrownStore — заказ №' + str(oid) + ' выполнен!\n\nТвои ключи:\n' + '\n'.join(keys)
    try:
        result = telegram_api('sendMessage', {'chat_id': user_id, 'text': msg})
        if not result or not result.get('ok'): raise RuntimeError('Telegram sendMessage failed')
    except Exception:
        app.logger.exception('Could not send keys to buyer')
        flash('Ключи закреплены за заказом, но сообщение не отправилось. Нажми повторную отправку.')
        return redirect(url_for('miniapp_admin'))
    with db() as c: c.execute("UPDATE orders SET status='Выполнен' WHERE id=?", (oid,))
    flash('Ключи отправлены покупателю в Telegram.')
    return redirect(url_for('miniapp_admin'))

@app.route('/api/my-purchases')
def my_purchases():
    user_id = verify_telegram_init_data(request.headers.get('X-Telegram-Init-Data', ''))
    if not user_id: return {'ok': False, 'error': 'Открой магазин через Telegram.'}, 401
    with db() as c:
        paid = c.execute("SELECT order_ids FROM star_payments WHERE telegram_user_id=? AND status='paid'", (user_id,)).fetchall()
        ids = {int(oid) for row in paid for oid in json.loads(row['order_ids'])}
        purchases = []
        for oid in ids:
            rows = c.execute('SELECT o.id order_id,o.created_at,p.name product_name,k.key_text FROM orders o JOIN product_keys k ON k.claimed_order_id=o.id JOIN products p ON p.id=o.product_id WHERE o.id=? ORDER BY k.id', (oid,)).fetchall()
            purchases.extend([dict(x) for x in rows])
    purchases.sort(key=lambda x:x['order_id'], reverse=True)
    return {'ok': True, 'purchases': purchases}

@app.route('/admin/product/<int:pid>/toggle',methods=['POST'])
@admin_required
def toggle_product(pid):
    with db() as c: c.execute('UPDATE products SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(pid,))
    return redirect(url_for('admin'))

@app.route('/admin/order/<int:oid>/status',methods=['POST'])
@admin_required
def update_order(oid):
    status=request.form.get('status','Новый')
    if status not in ['Ожидает оплаты','Оплачен','Новый','В обработке','Выполнен','Отменён']: abort(400)
    with db() as c: c.execute('UPDATE orders SET status=? WHERE id=?',(status,oid))
    return redirect(url_for('admin'))

init_db()
configure_telegram_webhook()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.getenv('PORT', '5000')),debug=False)
