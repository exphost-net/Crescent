from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
import mysql.connector
from mysql.connector import pooling
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, DecimalException
import os
import json
from dotenv import load_dotenv
import requests
import logging
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import threading
import time
import traceback
import socket

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_TYPE'] = 'filesystem'
app.logger.setLevel(logging.INFO)

paymenter_config = {
    'user': os.getenv('PAYMENTER_DB_USER'),
    'password': os.getenv('PAYMENTER_DB_PASSWORD'),
    'host': os.getenv('PAYMENTER_DB_HOST'),
    'database': os.getenv('PAYMENTER_DB_NAME')
}

pterodactyl_config = {
    'user': os.getenv('PTERODACTYL_DB_USER'),
    'password': os.getenv('PTERODACTYL_DB_PASSWORD'),
    'host': os.getenv('PTERODACTYL_DB_HOST'),
    'database': os.getenv('PTERODACTYL_DB_NAME')
}

COSTS_FILE = 'costs.json'
EXTRA_INCOME_FILE = 'extra_income.json'
USERS_FILE = 'users.json'

BASE_CURRENCY = os.getenv('BASE_CURRENCY', 'USD')

class CachedFile:
    def __init__(self, path, ttl=60):
        self.path = path
        self.ttl = ttl
        self._data = None
        self._last_load = 0
        self._lock = threading.Lock()
    def get(self):
        now = time.time()
        with self._lock:
            if self._data is None or now - self._last_load > self.ttl:
                try:
                    with open(self.path, 'r') as f:
                        self._data = json.load(f)
                except Exception:
                    self._data = {}
                self._last_load = now
            return self._data
    def set(self, data):
        with self._lock:
            with open(self.path, 'w') as f:
                json.dump(data, f, indent=4)
            self._data = data
            self._last_load = time.time()

exchange_rates_cache = CachedFile('exchange_rates.json', ttl=60)
costs_cache = CachedFile('costs.json', ttl=60)
extra_income_cache = CachedFile('extra_income.json', ttl=60)
users_cache = CachedFile('users.json', ttl=60)

def get_current_exchange_rates():
    data = exchange_rates_cache.get()
    rates = {}
    for currency, info in data.items():
        if isinstance(info, dict):
            rates[currency.upper()] = {
                "rate": Decimal(str(info.get("rate", "1.0"))),
                "symbol": info.get("symbol", currency)
            }
        else:
            rates[currency.upper()] = {
                "rate": Decimal(str(info)),
                "symbol": currency
            }
    if not rates:
        rates['USD'] = {"rate": Decimal('1.0'), "symbol": "$"}
    return rates

def convert_currency(amount, from_currency, to_currency, exchange_rates):
    from_currency_upper = from_currency.upper()
    to_currency_upper = to_currency.upper()
    if from_currency_upper == to_currency_upper:
        return amount
    from_rate_obj = exchange_rates.get(from_currency_upper)
    to_rate_obj = exchange_rates.get(to_currency_upper)
    if from_rate_obj is None or 'rate' not in from_rate_obj:
        return Decimal('0.00')
    if to_rate_obj is None or 'rate' not in to_rate_obj:
        return Decimal('0.00')
    from_rate = from_rate_obj['rate']
    to_rate = to_rate_obj['rate']
    try:
        converted_amount = amount * (to_rate / from_rate)
        return converted_amount.quantize(Decimal('0.01'), ROUND_HALF_UP)
    except Exception:
        return Decimal('0.00')

def load_costs(file_path):
    data = costs_cache.get()
    machine_costs = {}
    misc_costs = {}
    for k, v in data.get('machine_costs', {}).items():
        if isinstance(v, dict):
            machine_costs[k] = {
                'cost': to_decimal(v.get('cost', 0)),
                'billing_day': v.get('billing_day')
            }
        else:
            machine_costs[k] = {'cost': to_decimal(v), 'billing_day': None}
    misc_costs = {k: to_decimal(v) for k, v in data.get('misc_costs', {}).items()}
    return machine_costs, misc_costs

def save_costs(file_path, machine_costs, misc_costs):
    data = {
        'machine_costs': {
            k: {
                'cost': float(v['cost']),
                **({'billing_day': v['billing_day']} if v.get('billing_day') is not None else {})
            } for k, v in machine_costs.items()
        },
        'misc_costs': {k: float(v) for k, v in misc_costs.items()}
    }
    costs_cache.set(data)

def load_extra_income(file_path):
    data = extra_income_cache.get()
    return {k: to_decimal(v) for k, v in data.items()}

def save_extra_income(file_path, income_details):
    extra_income_cache.set({k: float(v) for k, v in income_details.items()})

def load_users():
    return users_cache.get()

def save_users(users):
    users_cache.set(users)

def to_decimal(value):
    if value is None:
        return Decimal('0.00')
    try:
        return Decimal(str(value).strip().replace(',', ''))
    except InvalidOperation:
        return Decimal('0.00')
    except Exception:
        return Decimal('0.00')

def is_admin(user_email):
    users = load_users()
    user_data = users.get(user_email)
    return user_data and user_data.get('is_admin', False)

app.jinja_env.globals['is_admin'] = is_admin

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_email' not in session or not is_admin(session['user_email']):
            flash('Unauthorized access. Please log in as an administrator.', 'danger')
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def home():
    if 'user_email' in session:
        return redirect(url_for('dashboard_page'))
    else:
        return redirect(url_for('login_page'))

@app.route('/incomings')
def incomings():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    pc = None
    pconn = None
    cur = None
    pcur = None
    email = session.get('user_email')
    user_is_admin_status = is_admin(email)

    node_income = {}
    service_details = {}
    unmatched_services = []
    unmatched_services_total = Decimal('0.00')
    total_cost = Decimal('0.00')
    total_income = Decimal('0.00')
    profit = Decimal('0.00')
    machine_costs = {}
    misc_costs = {}
    extra_income = Decimal('0.00')
    extra_income_details = {}
    exchange_rates = get_current_exchange_rates()

    try:
        pc = get_db_connection(paymenter_config, "Paymenter")
        if not pc:
            flash("Failed to connect to Paymenter database. Displaying limited data.", 'warning')
            return render_template('incomings.html',
                                   error="Failed to connect to Paymenter database. Income data unavailable.",
                                   email=email, node_income=node_income, service_details=service_details,
                                   unmatched_services=unmatched_services, unmatched_services_total=unmatched_services_total,
                                   total_cost=total_cost, total_income=total_income, profit=profit,
                                   machine_costs=machine_costs, cost_details=machine_costs, extra_income=extra_income,
                                   extra_income_details=extra_income_details, base_currency=BASE_CURRENCY,
                                   is_admin_status=user_is_admin_status)

        cur = pc.cursor(dictionary=True)

        if not exchange_rates:
            flash("Failed to load exchange rates. Income conversion may be inaccurate.", 'warning')

        cur.execute("SELECT id, product_id, user_id, status, price, currency_code, expires_at, coupon_id FROM services WHERE status IN ('active', 'suspended')")
        services = cur.fetchall()

        cur.execute("SELECT id, email FROM users")
        users_paymenter = cur.fetchall()
        user_email_map = {u['id']: u['email'] for u in users_paymenter}
        
        cur.execute("SELECT id, type, value FROM coupons")
        coupons = {c['id']: c for c in cur.fetchall()}

        pconn = get_db_connection(pterodactyl_config, "Pterodactyl")
        if not pconn:
            flash("Failed to connect to Pterodactyl database. Node and server matching unavailable.", 'warning')
            srv_lookup = {}
            node_map = {}
        else:
            pcur = pconn.cursor(dictionary=True)
            pcur.execute("SELECT id, name FROM nodes")
            node_map = {n['id']: n['name'] for n in pcur.fetchall()}
            pcur.execute("SELECT uuid, name, node_id, external_id FROM servers")
            srv_lookup = {s['external_id']: s for s in pcur.fetchall()}


        for svc in services:
            price = to_decimal(svc['price'])
            service_currency = svc.get('currency_code', BASE_CURRENCY)
            billing_date = svc.get('expires_at')
            coupon_id = svc.get('coupon_id')
            coupon_discount = None
            discounted_price = price

            if coupon_id and coupon_id in coupons:
                coupon = coupons[coupon_id]
                if coupon['type'] == 'percentage':
                    percent_off = Decimal(str(coupon['value']))
                    discounted_price = price * (Decimal('100') - percent_off) / Decimal('100')
                    coupon_discount = f"{percent_off}% OFF"
                elif coupon['type'] == 'fixed':
                    fixed_off = Decimal(str(coupon['value']))
                    discounted_price = max(price - fixed_off, Decimal('0.00'))
                    coupon_discount = f"{BASE_CURRENCY} {fixed_off} OFF"
            else:
                coupon_discount = None

            converted_price = convert_currency(discounted_price, service_currency, BASE_CURRENCY, exchange_rates)

            if not isinstance(converted_price, Decimal):
                logging.error(f"convert_currency returned non-Decimal value for service {svc['id']}. Defaulting to 0.00.")
                converted_price = Decimal('0.00')

            svc['price'] = converted_price.quantize(Decimal('0.01'), ROUND_HALF_UP)
            svc['currency'] = BASE_CURRENCY
            svc['original_price_str'] = f"{price:.2f} {service_currency}"

            user_email_for_service = user_email_map.get(svc['user_id'], 'Unknown User')

            srv = srv_lookup.get(str(svc['id']))

            if srv:
                node_name = node_map.get(srv['node_id'], 'Unknown Node')
                node_income.setdefault(node_name, Decimal('0.00'))
                node_income[node_name] += converted_price

                service_details.setdefault(node_name, []).append({
                    'service_id': svc['id'],
                    'user_email': user_email_for_service,
                    'price': svc['price'],
                    'currency': svc['currency'],
                    'original_price_str': svc['original_price_str'],
                    'server_uuid': srv.get('uuid', 'N/A'),
                    'server_name': srv.get('name', 'N/A'),
                    'status': svc['status'],
                    'billing_date': billing_date,
                    'coupon_discount': coupon_discount,
                })
            else:
                unmatched_services.append({
                    'service_id': svc['id'],
                    'user_email': user_email_for_service,
                    'price': svc['price'],
                    'currency': svc['currency'],
                    'original_price_str': svc['original_price_str'],
                    'status': svc['status'],
                    'billing_date': billing_date,
                    'coupon_discount': coupon_discount,
                })
                unmatched_services_total += converted_price

        machine_costs, misc_costs = load_costs(COSTS_FILE)
        extra_income_details = load_extra_income(EXTRA_INCOME_FILE)
        extra_income = sum(extra_income_details.values(), Decimal('0.00'))

        total_cost = sum((mc['cost'] for mc in machine_costs.values()), Decimal('0.00')) + sum(misc_costs.values(), Decimal('0.00'))
        total_income = sum(node_income.values(), Decimal('0.00')) + unmatched_services_total + extra_income
        profit = total_income - total_cost

        total_cost = total_cost.quantize(Decimal('0.01'), ROUND_HALF_UP)
        total_income = total_income.quantize(Decimal('0.01'), ROUND_HALF_UP)
        profit = profit.quantize(Decimal('0.01'), ROUND_HALF_UP)
        unmatched_services_total = unmatched_services_total.quantize(Decimal('0.01'), ROUND_HALF_UP)

        exchange_rates = get_current_exchange_rates()
        currency_symbol = exchange_rates.get(BASE_CURRENCY, {}).get("symbol", BASE_CURRENCY)

        return render_template('incomings.html',
                               node_income=node_income,
                               service_details=service_details,
                               unmatched_services=unmatched_services,
                               unmatched_services_total=unmatched_services_total,
                               total_cost=total_cost,
                               total_income=total_income,
                               profit=profit,
                               machine_costs=machine_costs,
                               misc_costs=misc_costs,
                               extra_income=extra_income,
                               extra_income_details=extra_income_details,
                               base_currency=BASE_CURRENCY,
                               email=email,
                               currency_symbol=currency_symbol,
                               exchange_rates=exchange_rates,
                               is_admin_status=user_is_admin_status)

    except mysql.connector.Error as db_err:
        logging.error(f"Database error in incomings route: {db_err}")
        flash("A database error occurred. Some data might be incomplete.", 'danger')
        return render_template('incomings.html',
                               error="A database error occurred.",
                               node_income=node_income, service_details=service_details, unmatched_services=unmatched_services,
                               unmatched_services_total=unmatched_services_total, total_cost=total_cost,
                               total_income=total_income, profit=profit, machine_costs=machine_costs,
                               misc_costs=misc_costs, extra_income=extra_income, extra_income_details=extra_income_details,
                               base_currency=BASE_CURRENCY, email=email, is_admin_status=user_is_admin_status)
    except Exception as e:
        logging.error(f"An unexpected error occurred in incomings route: {e}")
        traceback.print_exc()
        flash("An unexpected error occurred while processing your request.", 'danger')
        return render_template('incomings.html',
                               error="An unexpected error occurred.",
                               node_income=node_income, service_details=service_details, unmatched_services=unmatched_services,
                               unmatched_services_total=unmatched_services_total, total_cost=total_cost,
                               total_income=total_income, profit=profit, machine_costs=machine_costs,
                               misc_costs=misc_costs, extra_income=extra_income, extra_income_details=extra_income_details,
                               base_currency=BASE_CURRENCY, email=email, is_admin_status=user_is_admin_status)
    finally:
        if cur:
            cur.close()
        if pc and pc.is_connected():
            pc.close()
        if pcur:
            pcur.close()
        if pconn and pconn.is_connected():
            pconn.close()

@app.route('/outgoings', methods=['GET'])
def outgoings_page():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    email = session.get('user_email')
    user_is_admin_status = is_admin(email)

    pterodactyl_nodes = {}
    total_machine_outgoings = Decimal('0.00')
    total_misc_outgoings = Decimal('0.00')
    total_outgoings = Decimal('0.00')
    nodes_with_costs = {}

    try:
        pterodactyl_nodes = get_pterodactyl_nodes()
        if not pterodactyl_nodes:
            flash("Failed to load node data from Pterodactyl. Machine costs might be incomplete.", 'warning')

    except Exception as e:
        logging.error(f"Error getting Pterodactyl nodes in outgoings: {e}")
        flash("An error occurred while fetching Pterodactyl node data.", 'danger')

    machine_costs, misc_costs = load_costs(COSTS_FILE)

    if pterodactyl_nodes:
        orphaned_costs = set(machine_costs.keys()) - set(pterodactyl_nodes.keys())
        for orphaned_id in orphaned_costs:
            logging.info(f"Removing orphaned machine cost for node ID: {orphaned_id}")
            del machine_costs[orphaned_id]

        for node_id, node_name in pterodactyl_nodes.items():
            cost_obj = machine_costs.get(node_id)
            if cost_obj and isinstance(cost_obj, dict):
                cost_value = cost_obj.get('cost', Decimal('0.00'))
                billing_day = cost_obj.get('billing_day')
            else:
                cost_value = Decimal('0.00')
                billing_day = None
            nodes_with_costs[node_id] = {
                'name': node_name,
                'cost': cost_value,
                'billing_day': billing_day
            }

    if pterodactyl_nodes:
        save_costs(COSTS_FILE, machine_costs, misc_costs)

    total_machine_outgoings = sum((cost['cost'] for cost in nodes_with_costs.values()), Decimal('0.00'))
    total_misc_outgoings = sum(misc_costs.values(), Decimal('0.00'))
    total_outgoings = total_machine_outgoings + total_misc_outgoings

    exchange_rates = get_current_exchange_rates()
    currency_symbol = exchange_rates.get(BASE_CURRENCY, {}).get("symbol", BASE_CURRENCY)

    return render_template('outgoings.html',
                           machine_costs=nodes_with_costs,
                           misc_costs=misc_costs,
                           total_outgoings=total_outgoings.quantize(Decimal('0.01')),
                           total_machine_outgoings=total_machine_outgoings.quantize(Decimal('0.01')),
                           total_misc_outgoings=total_misc_outgoings.quantize(Decimal('0.01')),
                           base_currency=BASE_CURRENCY,
                           email=email,
                           currency_symbol=currency_symbol,
                           is_admin_status=user_is_admin_status)

@app.route('/update_machine_cost', methods=['POST'])
@admin_required
def update_machine_cost():
    node_id = request.form.get('node_id')
    cost_value = request.form.get('cost')
    billing_day = request.form.get('billing_day')
    if node_id and cost_value is not None:
        try:
            cost = to_decimal(cost_value).quantize(Decimal('0.01'), ROUND_HALF_UP)
            if cost is None:
                flash('Invalid cost value. Please enter a numeric value.', 'danger')
                return redirect(url_for('outgoings_page'))

            machine_costs, misc_costs = load_costs(COSTS_FILE)
            if node_id not in machine_costs:
                machine_costs[node_id] = {}
            machine_costs[node_id]['cost'] = cost
            if billing_day:
                machine_costs[node_id]['billing_day'] = int(billing_day)
            else:
                machine_costs[node_id].pop('billing_day', None)
            save_costs(COSTS_FILE, machine_costs, misc_costs)
            flash(f'Machine cost for node {node_id} updated successfully!', 'success')
            return redirect(url_for('outgoings_page'))
        except DecimalException:
            flash('Invalid cost value. Please enter a numeric value.', 'danger')
            return redirect(url_for('outgoings_page'))
        except Exception as e:
            logging.error(f"Error updating machine cost: {e}")
            flash('An error occurred while updating machine cost.', 'danger')
            return redirect(url_for('outgoings_page'))
    flash('Missing node ID or cost value.', 'danger')
    return redirect(url_for('outgoings_page'))

@app.route('/settings', methods=['GET'])
def settings_page():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    user_email = session.get('user_email')
    user_is_admin_status = is_admin(user_email)

    exchange_rates = get_current_exchange_rates()
    return render_template('settings.html',
                        is_admin_status=user_is_admin_status,
                        email=user_email,
                        exchange_rates=exchange_rates,
                        base_currency=BASE_CURRENCY)


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        users = load_users()

        if email in users and check_password_hash(users[email]['password'], password):
            session['user_email'] = email
            flash('Logged in successfully!', 'success')
            return redirect(url_for('dashboard_page'))
        else:
            flash('Invalid email or password', 'danger')
            return render_template('login.html', error='Invalid email or password')

    return render_template('login.html')

@app.route('/dashboard')
def dashboard_page():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    pc = None
    pconn = None
    cur = None
    pcur = None
    email = session.get('user_email')
    user_is_admin_status = is_admin(email)

    total_income = Decimal('0.00')
    total_cost = Decimal('0.00')
    profit = Decimal('0.00')
    total_servers = 0
    top_nodes_with_info = []
    total_allocated_memory_gb = "0.00"
    total_available_memory_gb = "0.00"
    memory_percent = 0.0
    total_allocated_disk_gb = "0.00"
    total_available_disk_gb = "0.00"
    disk_percent = 0.0
    exchange_rates = get_current_exchange_rates()

    try:
        pc = get_db_connection(paymenter_config, "Paymenter")
        if not pc:
            flash("Failed to connect to Paymenter database. Dashboard income data unavailable.", 'warning')
            return render_template('dashboard.html',
                                   error="Failed to connect to Paymenter database. Income data unavailable.",
                                   email=email, base_currency=BASE_CURRENCY,
                                   total_income=total_income, total_cost=total_cost, profit=profit,
                                   total_servers=total_servers, top_nodes=top_nodes_with_info,
                                   total_allocated_memory_gb=total_allocated_memory_gb,
                                   total_available_memory_gb=total_available_memory_gb, memory_percent=memory_percent,
                                   total_allocated_disk_gb=total_allocated_disk_gb,
                                   total_available_disk_gb=total_available_disk_gb, disk_percent=disk_percent,
                                   is_admin_status=user_is_admin_status)

        cur = pc.cursor(dictionary=True)
        if not exchange_rates:
            flash("Failed to load exchange rates. Income conversion may be inaccurate.", 'warning')

        cur.execute("SELECT id, product_id, user_id, status, price, currency_code, expires_at FROM services WHERE status IN ('active', 'suspended')")
        services = cur.fetchall()
        print("DASHBOARD DEBUG: Services data from Paymenter:", services)

        cur.execute("SELECT id, email FROM users")
        users_paymenter = cur.fetchall()
        user_email_map = {u['id']: u['email'] for u in users_paymenter}

        pconn = get_db_connection(pterodactyl_config, "Pterodactyl")
        if not pconn:
            flash("Failed to connect to Pterodactyl database. Server, node, and resource data unavailable.", 'warning')
            total_income = sum(convert_currency(to_decimal(svc['price']), svc.get('currency_code', BASE_CURRENCY), BASE_CURRENCY, exchange_rates)
                                for svc in services if to_decimal(svc['price']) is not None).quantize(Decimal('0.01'), ROUND_HALF_UP)
            
            machine_costs, misc_costs = load_costs(COSTS_FILE)
            extra_income_details = load_extra_income(EXTRA_INCOME_FILE)
            extra_income = sum(extra_income_details.values(), Decimal('0.00'))
            total_cost = (sum((mc['cost'] for mc in machine_costs.values()), Decimal('0.00')) + sum(misc_costs.values(), Decimal('0.00'))).quantize(Decimal('0.01'), ROUND_HALF_UP)
            profit = (total_income - total_cost).quantize(Decimal('0.01'), ROUND_HALF_UP)

            return render_template('dashboard.html',
                                   error="Failed to connect to Pterodactyl database. Server and node statistics unavailable.",
                                   email=email, base_currency=BASE_CURRENCY,
                                   total_income=total_income, total_cost=total_cost, profit=profit,
                                   total_servers=0, top_nodes=[],
                                   total_allocated_memory_gb=total_allocated_memory_gb,
                                   total_available_memory_gb=total_available_memory_gb, memory_percent=memory_percent,
                                   total_allocated_disk_gb=total_allocated_disk_gb,
                                   total_available_disk_gb=total_available_disk_gb, disk_percent=disk_percent,
                                   is_admin_status=user_is_admin_status)

        pcur = pconn.cursor(dictionary=True)
        pcur.execute("SELECT id, name, memory, disk, memory_overallocate, disk_overallocate FROM nodes")
        nodes_db_data = pcur.fetchall()
        node_id_to_info = {n['id']: n for n in nodes_db_data}
        node_id_to_name = {n['id']: n['name'] for n in nodes_db_data}
        print("DASHBOARD DEBUG: Node ID to Info Map from Pterodactyl:", node_id_to_info)
        print("DASHBOARD DEBUG: Node ID to Name Map from Pterodactyl:", node_id_to_name)

        pcur.execute("SELECT uuid, id, node_id, memory, disk, external_id FROM servers")
        servers_data = pcur.fetchall()
        print("DASHBOARD DEBUG: Servers data from Pterodactyl (including external_id):", servers_data)

        total_servers = len(servers_data)
        
        panel_url = os.getenv('PTERODACTYL_API_URL')
        api_key = os.getenv('PTERODACTYL_API_KEY')
        
        if not panel_url or not api_key:
            logging.warning("PTERODACTYL_API_URL or PTERODACTYL_API_KEY not set in .env. Wings service status will not be available.")
            headers = {}
            api_status_check_enabled = False
        else:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/vnd.pterodactyl.v1+json",
                "Content-Type": "application/json"
            }
            api_status_check_enabled = True

        node_revenue = {}
        node_costs_by_id = {}
        unmatched_total = Decimal('0.00')

        machine_costs, misc_costs = load_costs(COSTS_FILE)

        for node_id_str, cost_obj in machine_costs.items():
            try:
                node_id = int(node_id_str)
                if node_id in node_id_to_info:
                    node_costs_by_id[node_id] = to_decimal(cost_obj.get('cost', 0))
                else:
                    logging.warning(f"Cost found for unknown node ID: {node_id_str} in costs.json. It might be an orphaned cost.")
            except ValueError:
                logging.warning(f"Non-numeric key found in machine_costs that cannot be mapped to a node ID: {node_id_str}. Skipping.")
                pass

        for svc in services:
            price = to_decimal(svc['price'])
            service_currency = svc.get('currency_code', BASE_CURRENCY)
            
            converted_price = convert_currency(price, service_currency, BASE_CURRENCY, exchange_rates)
            if converted_price == Decimal('0.00') and price != Decimal('0.00'):
                logging.warning(f"Could not convert service {svc['id']} price {price} {service_currency} to {BASE_CURRENCY}. Skipping this service for dashboard income calculation.")
                print(f"DASHBOARD DEBUG: Skipping service {svc['id']} (product {svc.get('product_id', 'N/A')}) due to conversion failure to zero.")
                continue

            srv = next((s for s in servers_data if str(s.get('external_id')) == str(svc['id'])), None)

            print(f"\nDASHBOARD DEBUG: Processing Paymenter Service ID: {svc['id']} (Product ID: {svc.get('product_id', 'N/A')}), Price: {price} {service_currency} (Converted: {converted_price} {BASE_CURRENCY})")
            if srv:
                node_name_for_service = node_id_to_name.get(srv['node_id'], 'Unknown Node (ID not found)')
                print(f"    MATCHED to Pterodactyl Server UUID: {srv['uuid']}, Server ID: {srv['id']}, Node ID: {srv['node_id']}, Node Name: {node_name_for_service}")
            else:
                print(f"    UNMATCHED: No Pterodactyl server found for Paymenter service ID {svc['id']} (external_id).")

            if srv:
                node_id = srv['node_id']
                node_name = node_id_to_name.get(node_id, 'Unknown Node')
                node_revenue.setdefault(node_name, Decimal('0.00'))
                node_revenue[node_name] += converted_price
            else:
                unmatched_total += converted_price

        extra_income_details = load_extra_income(EXTRA_INCOME_FILE)
        extra_income = sum(extra_income_details.values(), Decimal('0.00'))

        total_cost = sum(node_costs_by_id.values(), Decimal('0.00')) + sum(misc_costs.values(), Decimal('0.00'))
        total_income = sum(node_revenue.values(), Decimal('0.00')) + unmatched_total + extra_income
        profit = total_income - total_cost

        total_cost = total_cost.quantize(Decimal('0.01'), ROUND_HALF_UP)
        total_income = total_income.quantize(Decimal('0.01'), ROUND_HALF_UP)
        profit = profit.quantize(Decimal('0.01'), ROUND_HALF_UP)

        for node in nodes_db_data:
            node_name = node['name']
            node_id = node['id']
            revenue = node_revenue.get(node_name, Decimal('0.00'))

            node_info = node_id_to_info.get(node_id, {'memory': 0, 'id': None, 'disk': 0,
                                                     'memory_overallocate': 0, 'disk_overallocate': 0})
            memory_mb = node_info.get('memory', 0)
            memory_gb = memory_mb / 1024

            is_online = False
            
            if api_status_check_enabled:
                panel_node_url = f"{panel_url}/api/application/nodes/{node_id}"
                try:
                    panel_response = requests.get(panel_node_url, headers=headers, timeout=5)
                    panel_response.raise_for_status()
                    node_data = panel_response.json().get('attributes', {})
                    wings_url = node_data.get('fqdn') or node_data.get('ip')
                    wings_port = node_data.get('daemon_listen')

                    if wings_url and wings_port is not None:
                        try:
                            sock = socket.create_connection((wings_url, int(wings_port)), timeout=1)
                            is_online = True
                            sock.close()
                        except (socket.error, socket.timeout) as e:
                            is_online = False
                            logging.warning(f"TCP connection to {wings_url}:{wings_port} for node {node_name}: Failed ({e})")
                    elif wings_url:
                        is_online = False 
                    else:
                        logging.warning(f"Could not find FQDN or IP for node {node_name} (ID: {node_id}) from Pterodactyl API for Wings status.")

                except requests.exceptions.Timeout:
                    logging.warning(f"Request to Pterodactyl API for node {node_name} (ID: {node_id}) timed out.")
                except requests.exceptions.ConnectionError:
                    logging.warning(f"Connection error to Pterodactyl API for node {node_name} (ID: {node_id}). API might be down or unreachable.")
                except requests.exceptions.HTTPError as e:
                    logging.warning(f"HTTP error from Pterodactyl API for node {node_name} (ID: {node_id}): {e.response.status_code} - {e.response.text}")
                except Exception as e:
                    logging.warning(f"Failed to fetch Panel details or check Wings status for node {node_name} (ID: {node_id}): {e}")

            node_cost = node_costs_by_id.get(node_id, Decimal('0.00'))
            node_profit = revenue - node_cost

            top_nodes_with_info.append({
                'name': node_name,
                'revenue': revenue.quantize(Decimal('0.01'), ROUND_HALF_UP),
                'costs': node_cost.quantize(Decimal('0.01'), ROUND_HALF_UP),
                'profit': node_profit.quantize(Decimal('0.01'), ROUND_HALF_UP),
                'memory': f"{memory_gb:.2f}",
                'online': is_online
            })
        top_nodes_with_info.sort(key=lambda x: x['revenue'], reverse=True)

        total_allocated_memory_mb = 0
        total_available_memory_mb = 0
        total_allocated_disk_mb = 0
        total_available_disk_mb = 0

        for node in nodes_db_data:
            node_id = node['id']
            node_physical_memory_mb = node.get('memory', 0) or 0
            node_memory_overallocate_percent = node.get('memory_overallocate', 0) or 0
            node_total_disk_mb = node.get('disk', 0) or 0
            node_disk_overallocate_percent = node.get('disk_overallocate', 0) or 0

            total_available_memory_mb += int(node_physical_memory_mb * (1 + (node_memory_overallocate_percent / 100)))
            total_available_disk_mb += int(node_total_disk_mb * (1 + (node_disk_overallocate_percent / 100)))

            for server in servers_data:
                if server['node_id'] == node_id:
                    total_allocated_memory_mb += server.get('memory', 0) or 0
                    total_allocated_disk_mb += server.get('disk', 0) or 0

        total_allocated_memory_gb = total_allocated_memory_mb / 1024
        total_available_memory_gb = total_available_memory_mb / 1024
        total_allocated_disk_gb = total_allocated_disk_mb / 1024
        total_available_disk_gb = total_available_disk_mb / 1024

        memory_percent = (total_allocated_memory_mb / total_available_memory_mb) * 100 if total_available_memory_mb > 0 else 0
        disk_percent = (total_allocated_disk_mb / total_available_disk_mb) * 100 if total_available_disk_mb > 0 else 0

        exchange_rates = get_current_exchange_rates()
        currency_symbol = exchange_rates.get(BASE_CURRENCY, {}).get("symbol", "$")

        return render_template('dashboard.html',
                               total_income=total_income,
                               total_cost=total_cost,
                               profit=profit,
                               email=email,
                               total_servers=total_servers,
                               top_nodes=top_nodes_with_info,
                               total_allocated_memory_gb=f"{total_allocated_memory_gb:.2f}",
                               total_available_memory_gb=f"{total_available_memory_gb:.2f}",
                               memory_percent=float(f"{memory_percent:.2f}"),
                               total_allocated_disk_gb=f"{total_allocated_disk_gb:.2f}",
                               total_available_disk_gb=f"{total_available_disk_gb:.2f}",
                               disk_percent=float(f"{disk_percent:.2f}"),
                               base_currency=BASE_CURRENCY,
                               is_admin_status=user_is_admin_status,
                               currency_symbol=currency_symbol
                               )
    except mysql.connector.Error as db_err:
        logging.error(f"Database error in dashboard_page route: {db_err}")
        flash("A database error occurred. Some data might be incomplete.", 'danger')
        return render_template('dashboard.html',
                               error="A database error occurred.",
                               email=email,
                               total_income=total_income,
                               total_cost=total_cost,
                               profit=profit,
                               total_servers=total_servers,
                               top_nodes=top_nodes_with_info,
                               total_allocated_memory_gb=total_allocated_memory_gb,
                               total_available_memory_gb=total_available_memory_gb,
                               memory_percent=memory_percent,
                               total_allocated_disk_gb=total_allocated_disk_gb,
                               total_available_disk_gb=total_available_disk_gb,
                               disk_percent=disk_percent,
                               base_currency=BASE_CURRENCY,
                               is_admin_status=user_is_admin_status)
    except Exception as e:
        logging.error(f"An unexpected error occurred in dashboard_page route: {e}")
        traceback.print_exc()
        flash("An error occurred while processing your request for the dashboard.", 'danger')
        return render_template('dashboard.html',
                               error="An error occurred while processing your request.",
                               email=email,
                               total_income=total_income,
                               total_cost=total_cost,
                               profit=profit,
                               total_servers=total_servers,
                               top_nodes=top_nodes_with_info,
                               total_allocated_memory_gb=total_allocated_memory_gb,
                               total_available_memory_gb=total_available_memory_gb,
                               memory_percent=memory_percent,
                               total_allocated_disk_gb=total_allocated_disk_gb,
                               total_available_disk_gb=total_available_disk_gb,
                               disk_percent=disk_percent,
                               base_currency=BASE_CURRENCY,
                               is_admin_status=user_is_admin_status)
    finally:
        if cur:
            cur.close()
        if pc and pc.is_connected():
            pc.close()
        if pcur:
            pcur.close()
        if pconn and pconn.is_connected():
            pconn.close()

@app.route('/logout')
def logout():
    session.pop('user_email', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('login_page'))

@app.route('/node_usage')
def node_usage():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    pconn = None
    pcur = None
    email = session.get('user_email')
    user_is_admin_status = is_admin(email)
    nodes_usage = []

    try:
        pconn = get_db_connection(pterodactyl_config, "Pterodactyl")
        if not pconn:
            flash("Failed to connect to the Pterodactyl database. Node usage data unavailable.", 'danger')
            return render_template('node_usage.html',
                                   error="Failed to connect to the Pterodactyl database. Node usage data unavailable.",
                                   email=email,
                                   nodes_usage=nodes_usage,
                                   is_admin_status=user_is_admin_status)

        pcur = pconn.cursor(dictionary=True)

        pcur.execute("""SELECT
                n.id AS node_id,
                n.name AS node_name,
                COALESCE(SUM(s.memory), 0) AS total_allocated_memory_mb,
                COALESCE(SUM(s.disk), 0) AS total_allocated_disk_mb,
                COUNT(s.id) AS server_count,
                n.memory AS node_physical_memory_mb,
                n.memory_overallocate AS node_memory_overallocate_percent,
                n.disk AS node_total_disk_mb,
                n.disk_overallocate AS node_disk_overallocate_percent
            FROM nodes n
            LEFT JOIN servers s ON n.id = s.node_id
            GROUP BY n.id, n.name, n.memory, n.disk, n.memory_overallocate, n.disk_overallocate
            ORDER BY n.name""")
        nodes_data = pcur.fetchall()

        for node in nodes_data:
            allocated_memory_mb = node['total_allocated_memory_mb']
            allocated_disk_mb = node['total_allocated_disk_mb']
            node_physical_memory_mb = node['node_physical_memory_mb'] or 0
            node_memory_overallocate_percent = node['node_memory_overallocate_percent'] or 0
            node_total_disk_mb = node['node_total_disk_mb'] or 0
            node_disk_overallocate_percent = node['node_disk_overallocate_percent'] or 0

            total_memory_for_display_mb = int(node_physical_memory_mb * (1 + (node_memory_overallocate_percent / 100)))
            total_disk_for_display_mb = int(node_total_disk_mb * (1 + (node_disk_overallocate_percent / 100)))

            allocated_memory_gb = allocated_memory_mb / 1024
            total_memory_for_display_gb = total_memory_for_display_mb / 1024
            allocated_disk_gb = allocated_disk_mb / 1024
            total_disk_for_display_gb = total_disk_for_display_mb / 1024

            memory_percent = (allocated_memory_mb / total_memory_for_display_mb) * 100 if total_memory_for_display_mb > 0 else 0
            disk_percent = (allocated_disk_mb / total_disk_for_display_mb) * 100 if total_disk_for_display_mb > 0 else 0
            node_name = node['node_name']

            nodes_usage.append({
                'node_name': node_name,
                'total_allocated_memory_gb': f"{allocated_memory_gb:.2f}",
                'allocated_memory_percent': float(f"{memory_percent:.2f}"),
                'total_allocated_disk_gb': f"{allocated_disk_gb:.2f}",
                'allocated_disk_percent': float(f"{disk_percent:.2f}"),
                'server_count': node['server_count'],
                'total_memory_for_display_gb': f"{total_memory_for_display_gb:.2f}",
                'total_disk_for_display_gb': f"{total_disk_for_display_gb:.2f}",
            })

        return render_template('node_usage.html',
                               nodes_usage=nodes_usage,
                               email=email,
                               is_admin_status=user_is_admin_status)
    except mysql.connector.Error as db_err:
        logging.error(f"Database error in node_usage route: {db_err}")
        flash("A database error occurred. Node usage data might be incomplete.", 'danger')
        return render_template('node_usage.html',
                               error="A database error occurred.",
                               email=email,
                               nodes_usage=nodes_usage,
                               is_admin_status=user_is_admin_status)
    except Exception as e:
        logging.error(f"Error in node_usage route: {e}")
        traceback.print_exc()
        flash("An error occurred while processing your request for node usage.", 'danger')
        return render_template('node_usage.html',
                               error="An error occurred while processing your request.",
                               email=email,
                               nodes_usage=nodes_usage,
                               is_admin_status=user_is_admin_status)
    finally:
        if pcur:
            pcur.close()
        if pconn and pconn.is_connected():
            pconn.close()

@app.route('/add_cost_ajax', methods=['POST'])
@admin_required
def add_cost_ajax():
    data = request.get_json()
    name = data.get('name')
    value = data.get('value')
    if name and value:
        decimal_value = to_decimal(value)
        if decimal_value is not None:
            machine_costs, misc_costs = load_costs(COSTS_FILE)
            if name in misc_costs:
                return jsonify({'success': False, 'error': 'Cost with this name already exists.'}), 409
            misc_costs[name] = decimal_value
            save_costs(COSTS_FILE, machine_costs, misc_costs)
            flash(f"Miscellaneous cost '{name}' added successfully.", 'success')
            return jsonify({'success': True, 'name': name, 'value': str(decimal_value)})
        else:
            return jsonify({'success': False, 'error': 'Invalid value provided. Please enter a numeric value.'}), 400
    else:
        return jsonify({'success': False, 'error': 'Missing data (name or value).'}), 400

@app.route('/remove_cost_ajax', methods=['POST'])
@admin_required
def remove_cost_ajax():
    data = request.get_json()
    name = data.get('name')
    if name and not name.startswith("NODE_"):
        machine_costs, misc_costs = load_costs(COSTS_FILE)
        if name in misc_costs:
            del misc_costs[name]
            save_costs(COSTS_FILE, machine_costs, misc_costs)
            flash(f"Miscellaneous cost '{name}' removed successfully.", 'success')
            return jsonify({'success': True, 'name': name})
        else:
            return jsonify({'success': False, 'error': 'Miscellaneous cost not found.'}), 404
    else:
        return jsonify({'success': False, 'error': 'Invalid or missing name, or attempting to remove a machine cost via this route.'}), 400

@app.route('/update_misc_cost_ajax', methods=['POST'])
@admin_required
def update_misc_cost_ajax():
    data = request.get_json()
    original_name = data.get('original_name')
    new_name = data.get('new_name')
    new_value = data.get('new_value')

    if original_name and new_name and new_value and not original_name.startswith("NODE_"):
        machine_costs, misc_costs = load_costs(COSTS_FILE)
        if original_name in misc_costs:
            decimal_value = to_decimal(new_value)
            if decimal_value is not None:
                if new_name != original_name and new_name in misc_costs:
                    return jsonify({'success': False, 'error': 'New name already exists.'}), 409

                if original_name != new_name:
                    del misc_costs[original_name]
                
                misc_costs[new_name] = decimal_value
                save_costs(COSTS_FILE, machine_costs, misc_costs)
                flash(f"Miscellaneous cost '{original_name}' updated to '{new_name}' with value {new_value}.", 'success')
                return jsonify({'success': True, 'old_name': original_name, 'new_name': new_name, 'new_value': str(decimal_value)})
            else:
                return jsonify({'success': False, 'error': 'Invalid value provided. Please enter a numeric value.'}), 400
        else:
            return jsonify({'success': False, 'error': 'Miscellaneous cost not found.'}), 404
    else:
        return jsonify({'success': False, 'error': 'Missing or invalid data.'}), 400

@app.route('/add_extra_income_ajax', methods=['POST'])
@admin_required
def add_extra_income_ajax():
    data = request.get_json()
    name = data.get('name')
    value = data.get('value')
    if name and value:
        decimal_value = to_decimal(value)
        if decimal_value is not None:
            extra_income_details = load_extra_income(EXTRA_INCOME_FILE)
            if name in extra_income_details:
                return jsonify({'success': False, 'error': 'Income source with this name already exists.'}), 409
            extra_income_details[name] = decimal_value
            save_extra_income(EXTRA_INCOME_FILE, extra_income_details)
            flash(f"Extra income source '{name}' added successfully.", 'success')
            return jsonify({'success': True, 'name': name, 'value': str(decimal_value)})
        else:
            return jsonify({'success': False, 'error': 'Invalid value provided. Please enter a numeric value.'}), 400
    else:
        return jsonify({'success': False, 'error': 'Missing data (name or value).'}), 400

@app.route('/remove_extra_income_ajax', methods=['POST'])
@admin_required
def remove_extra_income_ajax():
    data = request.get_json()
    name = data.get('name')
    if name:
        extra_income_details = load_extra_income(EXTRA_INCOME_FILE)
        if name in extra_income_details:
            del extra_income_details[name]
            save_extra_income(EXTRA_INCOME_FILE, extra_income_details)
            flash(f"Extra income source '{name}' removed successfully.", 'success')
            return jsonify({'success': True, 'name': name})
        else:
            return jsonify({'success': False, 'error': 'Income source not found.'}), 404
    else:
        return jsonify({'success': False, 'error': 'Missing data (name).'}), 400

@app.route('/update_income', methods=['POST'])
@admin_required
def update_income():
    data = request.get_json()
    original_name = data.get('original_name')
    new_name = data.get('new_name')
    new_value = data.get('new_value')

    if original_name is not None and new_name is not None and new_value is not None:
        income_details = load_extra_income(EXTRA_INCOME_FILE)
        if original_name in income_details:
            decimal_value = to_decimal(new_value)
            if decimal_value is not None:
                if new_name != original_name and new_name in income_details:
                    return jsonify({'success': False, 'error': 'New name already exists.'}), 409

                if original_name != new_name:
                    del income_details[original_name]

                income_details[new_name] = decimal_value
                save_extra_income(EXTRA_INCOME_FILE, income_details)
                flash(f"Extra income '{original_name}' updated to '{new_name}' with value {new_value}.", 'success')
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Invalid value provided. Please enter a numeric value.'}), 400
        else:
            return jsonify({'success': False, 'error': 'Income source not found.'}), 404
    else:
        return jsonify({'success': False, 'error': 'Missing data.'}), 400

@app.route('/update_password', methods=['POST'])
def update_password():
    if 'user_email' not in session:
        flash('Please log in to update your password.', 'warning')
        return redirect(url_for('login_page'))

    users = load_users()
    user_data = users.get(session['user_email'])

    if not user_data:
        flash('User not found. Please log in again.', 'danger')
        session.pop('user_email', None)
        return redirect(url_for('login_page'))

    old_password = request.form['old_password']
    new_password = request.form['new_password']
    confirm_password = request.form['confirm_password']

    if not check_password_hash(user_data['password'], old_password):
        flash('Incorrect old password.', 'danger')
        return render_template('settings.html', email=session.get('user_email'))

    if new_password != confirm_password:
        flash('New passwords do not match.', 'danger')
        return render_template('settings.html', email=session.get('user_email'))

    if old_password == new_password:
        flash('New password cannot be the same as the old password.', 'warning')
        return render_template('settings.html', email=session.get('user_email'))

    hashed_password = generate_password_hash(new_password)
    users[session['user_email']]['password'] = hashed_password
    save_users(users)
    flash('Password updated successfully!', 'success')
    return render_template('settings.html', email=session.get('user_email'))

@app.route('/update_email', methods=['POST'])
def update_email():
    if 'user_email' not in session:
        flash('Please log in to update your email.', 'warning')
        return redirect(url_for('login_page'))

    users = load_users()
    user_data = users.get(session['user_email'])

    if not user_data:
        flash('User not found. Please log in again.', 'danger')
        session.pop('user_email', None)
        return redirect(url_for('login_page'))

    current_password = request.form['current_password']
    new_email = request.form['new_email'].strip()

    if not check_password_hash(user_data['password'], current_password):
        flash('Incorrect password.', 'danger')
        return render_template('settings.html', email=session.get('user_email'))

    if new_email == session['user_email']:
        flash('New email cannot be the same as the current email.', 'warning')
        return render_template('settings.html', email=session.get('user_email'))

    if new_email in users:
        flash('This email address is already in use.', 'danger')
        return render_template('settings.html', email=session.get('user_email'))
    
    if not "@" in new_email or not "." in new_email:
        flash("Invalid email format.", 'danger')
        return render_template('settings.html', email=session.get('user_email'))

    users[new_email] = users.pop(session['user_email'])
    session['user_email'] = new_email
    save_users(users)
    flash('Email updated successfully!', 'success')
    return render_template('settings.html', email=session.get('user_email'))

@app.route('/update_exchange_rates', methods=['POST'])
@admin_required
def update_exchange_rates():
    try:
        new_rates = {}
        for currency, value in request.form.items():
            if currency.startswith('rate_'):
                code = currency.replace('rate_', '')
                try:
                    new_rates[code] = float(value)
                except ValueError:
                    flash(f"Invalid value for {code}.", 'danger')
                    return redirect(url_for('settings_page'))
        with open('exchange_rates.json', 'w') as f:
            json.dump(new_rates, f, indent=4)
        flash("Exchange rates updated successfully!", "success")
    except Exception as e:
        logging.error(f"Error updating exchange rates: {e}")
        flash("Failed to update exchange rates.", "danger")
    return redirect(url_for('settings_page'))

@app.route('/admin')
@admin_required
def admin_page():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    users_data_from_db = load_users()
    users_list_for_template = [{'email': email, 'is_admin': data.get('is_admin', False)} for email, data in users_data_from_db.items()]

    user_email = session.get('user_email')
    user_is_admin_status = is_admin(user_email)

    return render_template('admin.html',
                           users=users_list_for_template,
                           email=user_email,
                           is_admin_status=user_is_admin_status)

@app.route('/admin/add_user', methods=['POST'])
@admin_required
def add_user():
    email = request.form['email'].strip()
    password = request.form['password']
    is_admin_checkbox = request.form.get('is_admin')

    if not email or not password:
        flash('Email and password are required to add a user.', 'danger')
        return redirect(url_for('admin_page'))
    
    if not "@" in email or not "." in email:
        flash("Invalid email format for new user.", 'danger')
        return redirect(url_for('admin_page'))

    users = load_users()
    if email in users:
        flash(f"User '{email}' already exists.", 'warning')
        return redirect(url_for('admin_page'))
    else:
        hashed_password = generate_password_hash(password)
        is_admin_status = True if is_admin_checkbox else False
        users[email] = {'password': hashed_password, 'is_admin': is_admin_status}
        save_users(users)
        flash(f"User '{email}' added successfully!", 'success')
    return redirect(url_for('admin_page'))

@app.route('/admin/update_password', methods=['POST'])
@admin_required
def admin_update_password():
    email_to_update = request.form.get('email').strip()
    new_password = request.form['password']
    
    if not email_to_update or not new_password:
        flash('Email and new password are required to update password.', 'danger')
        return redirect(url_for('admin_page'))

    users = load_users()
    if email_to_update in users:
        hashed_password = generate_password_hash(new_password)
        users[email_to_update]['password'] = hashed_password
        save_users(users)
        flash(f"Password for user '{email_to_update}' updated successfully!", 'success')
    else:
        flash(f"User '{email_to_update}' not found.", 'danger')
    return redirect(url_for('admin_page'))

@app.route('/admin/make_admin', methods=['POST'])
@admin_required
def make_admin():
    email_to_admin = request.form.get('email').strip()
    users = load_users()
    if email_to_admin in users:
        current_admin_status = users[email_to_admin].get('is_admin', False)
        users[email_to_admin]['is_admin'] = not current_admin_status
        save_users(users)
        action = "granted admin rights to" if not current_admin_status else "revoked admin rights from"
        flash(f"Successfully {action} user '{email_to_admin}'.", 'success')
    else:
        flash(f"User '{email_to_admin}' not found.", 'danger')
    return redirect(url_for('admin_page'))

@app.route('/admin/remove_user', methods=['POST'])
@admin_required
def remove_user():
    email_to_remove = request.form.get('email').strip()
    
    if not email_to_remove:
        flash("No email provided for removal.", 'danger')
        return redirect(url_for('admin_page'))

    if email_to_remove == session.get('user_email'):
        flash("You cannot remove your own account while logged in.", 'danger')
        return redirect(url_for('admin_page'))
    
    users = load_users()
    if email_to_remove in users:
        del users[email_to_remove]
        save_users(users)
        flash(f"User '{email_to_remove}' removed successfully.", 'success')
    else:
        flash(f"User '{email_to_remove}' not found.", 'danger')
    return redirect(url_for('admin_page'))

@app.route('/add_exchange_rate_ajax', methods=['POST'])
@admin_required
def add_exchange_rate_ajax():
    data = request.get_json()
    code = data.get('code', '').upper()
    rate = data.get('rate')
    symbol = data.get('symbol', '')
    try:
        rate = float(rate)
        with open('exchange_rates.json', 'r') as f:
            rates = json.load(f)
        if code in rates:
            return jsonify({'success': False, 'error': 'Currency already exists.'}), 409
        rates[code] = {"rate": rate, "symbol": symbol}
        with open('exchange_rates.json', 'w') as f:
            json.dump(rates, f, indent=4)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/remove_exchange_rate_ajax', methods=['POST'])
@admin_required
def remove_exchange_rate_ajax():
    data = request.get_json()
    code = data.get('code', '').upper()
    try:
        with open('exchange_rates.json', 'r') as f:
            rates = json.load(f)
        if code not in rates:
            return jsonify({'success': False, 'error': 'Currency not found.'}), 404
        del rates[code]
        with open('exchange_rates.json', 'w') as f:
            json.dump(rates, f, indent=4)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/update_exchange_rate_ajax', methods=['POST'])
@admin_required
def update_exchange_rate_ajax():
    data = request.get_json()
    original_code = data.get('original_code', '').upper()
    new_code = data.get('new_code', '').upper()
    new_rate = data.get('new_rate')
    new_symbol = data.get('new_symbol', '')
    try:
        new_rate = float(new_rate)
        with open('exchange_rates.json', 'r') as f:
            rates = json.load(f)
        if original_code not in rates:
            return jsonify({'success': False, 'error': 'Original currency not found.'}), 404
        if new_code != original_code and new_code in rates:
            return jsonify({'success': False, 'error': 'New currency code already exists.'}), 409
        del rates[original_code]
        rates[new_code] = {"rate": new_rate, "symbol": new_symbol}
        with open('exchange_rates.json', 'w') as f:
            json.dump(rates, f, indent=4)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    
@app.route('/set_base_currency', methods=['POST'])
@admin_required
def set_base_currency():
    data = request.get_json()
    new_base = data.get('base_currency', '').upper()
    try:
        env_path = '.env'
        lines = []
        found = False
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    if line.startswith('BASE_CURRENCY='):
                        lines.append(f'BASE_CURRENCY={new_base}\n')
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f'BASE_CURRENCY={new_base}\n')
        with open(env_path, 'w') as f:
            f.writelines(lines)
        global BASE_CURRENCY
        BASE_CURRENCY = new_base
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

mysql_pool = pooling.MySQLConnectionPool(
    pool_name="mypool",
    pool_size=10,
    user=paymenter_config['user'],
    password=paymenter_config['password'],
    host=paymenter_config['host'],
    database=paymenter_config['database'],
    autocommit=True
)

def get_db_connection(config, connection_name=""):
    try:
        if connection_name == "Paymenter":
            connection = mysql_pool.get_connection()
        else:
            connection = mysql.connector.connect(
                user=config['user'],
                password=config['password'],
                host=config['host'],
                database=config['database'],
                autocommit=True
            )
        if connection.is_connected():
            logging.info(f"Successfully connected to {connection_name} database")
            return connection
    except mysql.connector.Error as err:
        logging.error(f"Error connecting to {connection_name} database: {err}")
    except Exception as e:
        logging.error(f"An unexpected error occurred while connecting to {connection_name} database: {e}")
    return None

def get_pterodactyl_nodes():
    conn = None
    cur = None
    nodes = {}
    try:
        conn = get_db_connection(pterodactyl_config, "Pterodactyl")
        if not conn:
            logging.error("Could not connect to Pterodactyl database in get_pterodactyl_nodes.")
            return {}
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, name FROM nodes")
        for row in cur.fetchall():
            nodes[str(row['id'])] = row['name']
    except Exception as e:
        logging.error(f"Error in get_pterodactyl_nodes: {e}")
    finally:
        if cur:
            cur.close()
        if conn and conn.is_connected():
            conn.close()
    return nodes

if __name__ == '__main__':
    if not os.path.exists(USERS_FILE) or os.stat(USERS_FILE).st_size == 0:
        default_admin_email = os.getenv('DEFAULT_ADMIN_EMAIL')
        default_admin_password = os.getenv('DEFAULT_ADMIN_PASSWORD')
        if default_admin_email and default_admin_password:
            hashed_password = generate_password_hash(default_admin_password)
            default_users = {
                default_admin_email: {'password': hashed_password, 'is_admin': True}
            }
            save_users(default_users)
            print(f"Created default 'users.json' with an admin user from .env.")
        else:
            print("Warning: DEFAULT_ADMIN_EMAIL and/or DEFAULT_ADMIN_PASSWORD not set in .env. No default admin user created.")
    app.run(debug=True, host='127.0.0.1')