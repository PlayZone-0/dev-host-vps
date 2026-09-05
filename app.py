from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import json
import os
import subprocess
import random
import string
import uuid
import re
from datetime import datetime, timedelta
import sys
import shutil
import threading
import time
import zipfile
import psutil

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key-in-production')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# DEV HOSTING administrator credentials
ADMIN_USERNAME = 'devxd'
ADMIN_PASSWORD = 'devxd'

USERS_FILE = 'users.json'
BOTS_DIR = 'bots'
CPU_HISTORY = {}
CRASH_COUNT = {}
NET_STATS = {}

os.makedirs(BOTS_DIR, exist_ok=True)

# ============================================
# রেট লিমিট
# ============================================

class RateLimiter:
    def check_rate(self, server_id, limit_percent):
        if server_id not in CPU_HISTORY:
            CPU_HISTORY[server_id] = []
        users = load_users()
        server = None
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    server = s
                    break
        if not server or server.get('status') != 'running':
            return False, 0
        pid = server.get('pid')
        if not pid: return False, 0
        try:
            proc = psutil.Process(pid)
            cpu = proc.cpu_percent(interval=1)
            now = time.time()
            CPU_HISTORY[server_id].append({'time': now, 'cpu': cpu})
            CPU_HISTORY[server_id] = [h for h in CPU_HISTORY[server_id] if now - h['time'] < 30]
            recent = [h['cpu'] for h in CPU_HISTORY[server_id] if now - h['time'] < 10]
            if recent:
                avg_cpu = sum(recent) / len(recent)
                if avg_cpu > limit_percent:
                    return True, avg_cpu
        except: pass
        return False, 0

rate_limiter = RateLimiter()

# ============================================
# অটো-রিস্টার্ট
# ============================================

def should_auto_restart(server_id):
    if server_id not in CRASH_COUNT:
        CRASH_COUNT[server_id] = {'count': 0, 'last_crash': time.time()}
    crash_info = CRASH_COUNT[server_id]
    if time.time() - crash_info['last_crash'] < 60:
        if crash_info['count'] >= 3:
            return False
    else:
        crash_info['count'] = 0
    crash_info['count'] += 1
    crash_info['last_crash'] = time.time()
    return True

# ============================================
# হেল্পার
# ============================================

def generate_random_password(length=10):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))

def load_users():
    if not os.path.exists(USERS_FILE):
        default = {"admin": {"password": ADMIN_PASSWORD, "role": "admin", "display_username": ADMIN_USERNAME}}
        save_users(default)
        return default
    with open(USERS_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if 'admin' not in data:
        data['admin'] = {"password": ADMIN_PASSWORD, "role": "admin", "display_username": ADMIN_USERNAME}
        save_users(data)
    return data

def save_users(data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def get_server_dir(server_id):
    server_dir = os.path.join(BOTS_DIR, server_id)
    os.makedirs(server_dir, exist_ok=True)
    return server_dir

def check_server_valid(server_id):
    users = load_users()
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                expiry = s.get('expiry', '')
                if expiry:
                    try:
                        exp_date = datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S.%f')
                        if datetime.now() > exp_date:
                            return False, "expired"
                    except: pass
                return True, s
    return False, "deleted"

def get_server_by_id(server_id):
    users = load_users()
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                return s, uname
    return None, None

def detect_start_file(server_dir):
    """Choose the bot entry file automatically; no Startup setting is needed."""
    preferred = ['main.py', 'app.py', 'bot.py', 'run.py', 'index.py']
    for name in preferred:
        if os.path.isfile(os.path.join(server_dir, name)):
            return name
    try:
        py_files = sorted(
            f for f in os.listdir(server_dir)
            if f.lower().endswith('.py') and os.path.isfile(os.path.join(server_dir, f))
        )
        return py_files[0] if py_files else None
    except OSError:
        return None

def get_requirements_file(server_dir):
    path = os.path.join(server_dir, 'requirements.txt')
    return 'requirements.txt' if os.path.isfile(path) else None

def create_default_files(server_dir):
    main_py = os.path.join(server_dir, 'main.py')
    if not os.path.exists(main_py):
        with open(main_py, 'w', encoding='utf-8') as f:
            f.write('''# DEV HOSTING - Default Bot
import time

print("=" * 40)
print("Bot is running on DEV HOSTING")
print("Server is ready!")
print("=" * 40)

counter = 0
while True:
    counter += 1
    print(f"[{time.strftime('%H:%M:%S')}] Heartbeat #{counter} | Server active")
    time.sleep(10)
''')
    
    req_file = os.path.join(server_dir, 'requirements.txt')
    if not os.path.exists(req_file):
        with open(req_file, 'w', encoding='utf-8') as f:
            f.write('# Add your pip packages here\n')

# ============================================
# বট রান
# ============================================

def run_bot(server_id, main_file='main.py', requirements_file='requirements.txt'):
    server_dir = get_server_dir(server_id)
    if not main_file or not os.path.isfile(os.path.join(server_dir, main_file)):
        main_file = detect_start_file(server_dir)
    if not main_file:
        return None, 'No Python entry file found. Upload a .py file first.'
    requirements_file = get_requirements_file(server_dir)
    main_path = os.path.join(server_dir, main_file)
    log_file = os.path.join(server_dir, 'output.log')
    python_exe = sys.executable
    
    def log(msg):
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"{msg}\n")
                f.flush()
        except: pass
    
    if not os.path.exists(main_path):
        return None, f"ERROR: {main_file} not found!"
    
    if os.path.exists(log_file):
        try: os.remove(log_file)
        except: open(log_file, 'w').close()
    
    ts = lambda: datetime.now().strftime('%I:%M:%S %p')
    
    server, _ = get_server_by_id(server_id)
    cpu_limit = server.get('cpu_limit', 80) if server else 80
    log(f"[{ts()}] Checking rate limit...")
    log(f"[{ts()}] Rate limit: {cpu_limit}%")
    log("")
    
    if requirements_file and requirements_file.strip():
        req_path = os.path.join(server_dir, requirements_file.strip())
        log(f"[{ts()}] Run: pip install -r {requirements_file}")
        log("")
        
        if os.path.exists(req_path):
            with open(req_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            lines = [l.strip() for l in content.split('\n') if l.strip() and not l.strip().startswith('#')]
            
            if lines:
                try:
                    proc = subprocess.Popen(
                        [python_exe, '-m', 'pip', 'install', '-r', os.path.abspath(req_path), '--disable-pip-version-check'],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, universal_newlines=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    
                    for line in iter(proc.stdout.readline, ''):
                        if line.strip():
                            log(f"[{ts()}] {line.rstrip()}")
                    
                    proc.wait()
                    log("")
                    
                    if proc.returncode != 0:
                        log(f"[{ts()}] Some packages failed to install")
                    else:
                        log(f"[{ts()}] Requirements installation complete!")
                except Exception as e:
                    log(f"[{ts()}] pip error: {str(e)}")
            else:
                log(f"[{ts()}] {requirements_file} is empty, skipping...")
        else:
            log(f"[{ts()}] {requirements_file} not found, skipping...")
    else:
        log(f"[{ts()}] No requirements file set, skipping...")
    
    log("")
    log(f"[{ts()}] Run: python {main_file}")
    log(f"[{ts()}] Python {sys.version.split()[0]}")
    log("")
    
    try:
        main_path_abs = os.path.abspath(main_path)
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        
        proc = subprocess.Popen(
            [python_exe, main_path_abs],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=server_dir,
            text=True, encoding='utf-8', errors='replace',
            bufsize=1, env=env, universal_newlines=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        
        log(f"[{ts()}] Server marked as running")
        log(f"[{ts()}] PID: {proc.pid}")
        log("")
        
        def rate_monitor():
            while proc.poll() is None:
                time.sleep(5)
                exceeded, avg_cpu = rate_limiter.check_rate(server_id, cpu_limit)
                if exceeded:
                    log(f"[{datetime.now().strftime('%I:%M:%S %p')}] CPU Limit! {avg_cpu:.1f}% > {cpu_limit}%")
                    proc.terminate()
                    time.sleep(2)
                    if proc.poll() is None: proc.kill()
                    
                    users = load_users()
                    for uname, data in users.items():
                        if uname == 'admin': continue
                        servers = data.get('servers', [])
                        if not isinstance(servers, list): continue
                        for s in servers:
                            if isinstance(s, dict) and s.get('server_id') == server_id:
                                s['status'] = 'stopped'
                                s['pid'] = None
                                s['rate_limit_exceeded'] = True
                                s['stopped_by_user'] = False
                                save_users(users)
                                break
                    break
        
        threading.Thread(target=rate_monitor, daemon=True).start()
        
        def stream_output():
            try:
                with open(log_file, 'a', encoding='utf-8') as f:
                    for line in iter(proc.stdout.readline, ''):
                        if line:
                            line = line.rstrip('\n\r')
                            if line:
                                f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] {line}\n")
                                f.flush()
            except: pass
        
        threading.Thread(target=stream_output, daemon=True).start()
        
        return proc.pid, None
        
    except Exception as e:
        log(f"[{ts()}] Error: {str(e)}")
        return None, str(e)

def stop_bot_process(pid):
    try:
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        else:
            os.kill(pid, 15)
        return True
    except: return False

def monitor_bot(server_id, pid):
    while True:
        try:
            if sys.platform == 'win32':
                result = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'], capture_output=True, text=True)
                if str(pid) not in result.stdout:
                    break
            else:
                try: os.kill(pid, 0)
                except: break
        except: break
        time.sleep(5)
    
    server, _ = get_server_by_id(server_id)
    if not server: return
    if server.get('stopped_by_user'): return
    if server.get('rate_limit_exceeded'): return
    
    if should_auto_restart(server_id):
        time.sleep(3)
        new_pid, error = run_bot(server_id, detect_start_file(get_server_dir(server_id)),
                                 get_requirements_file(get_server_dir(server_id)))
        if new_pid:
            users = load_users()
            for uname, data in users.items():
                if uname == 'admin': continue
                servers = data.get('servers', [])
                if not isinstance(servers, list): continue
                for s in servers:
                    if isinstance(s, dict) and s.get('server_id') == server_id:
                        s['status'] = 'running'
                        s['pid'] = new_pid
                        s['started_at'] = str(datetime.now())
                        s['rate_limit_exceeded'] = False
                        s['stopped_by_user'] = False
                        save_users(users)
                        break
            threading.Thread(target=monitor_bot, args=(server_id, new_pid), daemon=True).start()
    else:
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    s['status'] = 'stopped'
                    s['pid'] = None
                    save_users(users)
                    return

def get_process_stats(pid):
    try:
        proc = psutil.Process(pid)
        cpu = proc.cpu_percent(interval=0.5)
        mem = proc.memory_info()
        ram = mem.rss / (1024 * 1024)
        return {
            'cpu_percent': round(cpu, 1),
            'ram_mb': round(ram, 1),
            'ram_display': f"{ram:.1f} MB" if ram < 1024 else f"{ram/1024:.1f} GB",
        }
    except:
        return {'cpu_percent': 0, 'ram_mb': 0, 'ram_display': '0 MB'}

def get_network_stats(psutil_pid):
    try:
        proc = psutil.Process(psutil_pid)
        io = proc.io_counters()
        if io:
            read_kb = io.read_bytes / 1024
            write_kb = io.write_bytes / 1024
            return format_bytes(read_kb), format_bytes(write_kb)
    except: pass
    return "0 KB", "0 KB"

def format_bytes(kb):
    if kb < 1024: return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024: return f"{mb:.1f} MB"
    gb = mb / 1024
    return f"{gb:.2f} GB"

# ============================================
# 🔥 পাবলিক API - সার্ভার তৈরি
# ============================================

@app.route('/api/create', methods=['GET'])
def api_create_server():
    username = request.args.get('username', '').strip()
    server_name = request.args.get('name', '').strip()
    password = request.args.get('password', '').strip()
    server_type = request.args.get('type', 'python').strip()
    ram = request.args.get('ram', '1GB').strip()
    disk = request.args.get('disk', '1GB').strip()
    cpu_limit = int(request.args.get('cpu', '30'))
    days = int(request.args.get('days', '3'))
    
    if not server_name:
        return jsonify({'status': 'error', 'message': 'Server name is required!'}), 400

    if len(server_name) > 40:
        return jsonify({'status': 'error', 'message': 'Server name must be 40 characters or less!'}), 400

    if not password:
        password = generate_random_password(10)
    
    if not username:
        username = f"DEVHOST{random.randint(10000, 99999)}"
    
    if len(username) < 3:
        return jsonify({'status': 'error', 'message': 'Username must be at least 3 characters!'}), 400
    
    if len(password) < 4:
        return jsonify({'status': 'error', 'message': 'Password must be at least 4 characters!'}), 400
    
    if cpu_limit < 10 or cpu_limit > 100:
        return jsonify({'status': 'error', 'message': 'CPU limit must be between 10 and 100!'}), 400
    
    if days < 1 or days > 365:
        return jsonify({'status': 'error', 'message': 'Days must be between 1 and 365!'}), 400
    
    users = load_users()
    
    if username in users:
        return jsonify({'status': 'error', 'message': f"Username '{username}' already exists!"}), 400
    
    server_id = str(uuid.uuid4())[:8]
    expiry_date = datetime.now() + timedelta(days=days)
    
    create_default_files(get_server_dir(server_id))
    
    host = request.host
    is_local = host.startswith('localhost') or host.startswith('127.0.0.1') or host.startswith('192.168')
    scheme = 'http' if is_local else 'https'
    full_url = f"{scheme}://{host}/{server_id}/login"
    
    new_server = {
        'server_id': server_id,
        'name': server_name,
        'login_url': f"/{server_id}/login",
        'dashboard_url': f"/{server_id}/home",
        'full_link': full_url,
        'type': server_type,
        'ram': ram, 'disk': disk,
        'status': 'stopped', 'pid': None,
        'created': str(datetime.now()),
        'expiry': str(expiry_date),
        'main_file': None,
        'requirements_file': None,
        'cpu_limit': cpu_limit,
        'rate_limit_exceeded': False,
        'stopped_by_user': False
    }
    
    users[username] = {'password': password, 'role': 'user', 'servers': [new_server]}
    save_users(users)
    
    return jsonify({
        'status': 'success',
        'message': 'Panel created successfully!',
        'username': username,
        'password': password,
        'server_name': server_name,
        'server_type': server_type,
        'ram': ram,
        'disk': disk,
        'cpu_limit': cpu_limit,
        'validity': f'{days} days',
        'expiry_date': expiry_date.strftime('%Y-%m-%d'),
        'full_url': full_url,
        'server_id': server_id
    }), 200

# ============================================
# AUTH HELPERS
# ============================================

def require_server_access(server_id):
    if session.get('role') == 'admin':
        return True
    if session.get('role') != 'user' or session.get('current_server_id') != server_id:
        return False
    valid, _ = check_server_valid(server_id)
    return bool(valid)

def json_unauthorized():
    return jsonify({'status': 'error', 'msg': 'Unauthorized'}), 403

# ============================================
# রাউটস
# ============================================

@app.route('/')
def index():
    return render_template('landing.html')

@app.route('/landing')
def landing():
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        users = load_users()
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['user'] = 'admin'
            session['display_username'] = ADMIN_USERNAME
            session['role'] = 'admin'
            return redirect(url_for('admin_dashboard'))
        return render_template('login.html', error="Invalid credentials!")
    return render_template('login.html', error=None)

@app.route('/<server_id>/login', methods=['GET', 'POST'])
def server_login(server_id):
    valid, result = check_server_valid(server_id)
    if not valid:
        return render_template('error.html', error_type=result if result else "deleted", server_link=server_id)
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    if username == uname and password == data.get('password'):
                        session['user'] = uname
                        session['role'] = 'user'
                        session['current_server_id'] = server_id
                        return redirect(url_for('server_home', server_id=server_id))
                    else:
                        return render_template('login.html', error="Invalid credentials!")
        return render_template('login.html', error="Invalid login!")
    return render_template('login.html', error=None)

@app.route('/<server_id>/home')
def server_home(server_id):
    if 'user' not in session or session.get('role') != 'user':
        return redirect(url_for('server_login', server_id=server_id))
    if session.get('current_server_id') != server_id:
        session.clear()
        return redirect(url_for('server_login', server_id=server_id))
    
    valid, result = check_server_valid(server_id)
    if not valid:
        session.clear()
        return render_template('error.html', error_type=result if result else "deleted", server_link=server_id)
    
    return render_template('home.html', username=session['user'], current_server=result)

@app.route('/logout')
def logout():
    server_id = session.get('current_server_id')
    session.clear()
    if server_id:
        return redirect(url_for('server_login', server_id=server_id))
    return redirect(url_for('login'))

# ============================================
# অ্যাডমিন
# ============================================

@app.route('/admin')
def admin_dashboard():
    if 'user' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    users = load_users()
    user_list = []
    total_servers = 0
    total_running = 0
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): servers = []
        running = sum(1 for s in servers if isinstance(s, dict) and s.get('status') == 'running')
        total_servers += len(servers)
        total_running += running
        user_list.append({
            'username': uname, 'password': data.get('password', ''),
            'servers': servers, 'server_count': len(servers), 'running_count': running
        })
    return render_template('admin.html', users=user_list, total_servers=total_servers, total_running=total_running, admin_username=ADMIN_USERNAME)

@app.route('/admin/create_server', methods=['POST'])
def create_server():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    server_name = data.get('server_name', '').strip()
    server_type = data.get('server_type', 'python')
    ram = data.get('ram', '512MB')
    disk = data.get('disk', '1GB')
    expiry_days = int(data.get('expiry_days', 30))
    cpu_limit = int(data.get('cpu_limit', 80))
    
    if not username or not password or not server_name:
        return jsonify({'error': 'Server name, username and password are required!'}), 400
    if len(server_name) > 40:
        return jsonify({'error': 'Server name must be 40 characters or less!'}), 400
    
    users = load_users()
    server_id = str(uuid.uuid4())[:8]
    expiry_date = datetime.now() + timedelta(days=expiry_days)
    
    create_default_files(get_server_dir(server_id))
    
    new_server = {
        'server_id': server_id, 'name': server_name, 'link': server_id,
        'login_url': f"/{server_id}/login",
        'dashboard_url': f"/{server_id}/home",
        'full_link': request.host_url.rstrip('/') + f"/{server_id}/home",
        'type': server_type, 'ram': ram, 'disk': disk,
        'status': 'stopped', 'pid': None,
        'created': str(datetime.now()), 'expiry': str(expiry_date),
        'main_file': None, 'requirements_file': None,
        'cpu_limit': cpu_limit, 'rate_limit_exceeded': False, 'stopped_by_user': False
    }
    
    if username not in users:
        users[username] = {'password': password, 'role': 'user', 'servers': []}
    
    users[username]['servers'].append(new_server)
    save_users(users)
    
    return jsonify({
        'success': True, 'username': username, 'password': password,
        'login_url': new_server['login_url'],
        'hostname': new_server['full_link'],
        'server_id': server_id
    })

@app.route('/admin/set_rate_limit/<server_id>', methods=['POST'])
def set_rate_limit(server_id):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    cpu_limit = int(request.get_json().get('cpu_limit', 80))
    users = load_users()
    for uname, udata in users.items():
        if uname == 'admin': continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['cpu_limit'] = cpu_limit
                save_users(users)
                return jsonify({'success': True, 'cpu_limit': cpu_limit})
    return jsonify({'error': 'Not found'}), 404

@app.route('/admin/delete_server/<username>/<server_id>', methods=['POST'])
def delete_server(username, server_id):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    users = load_users()
    if username in users:
        servers = users[username].get('servers', [])
        if not isinstance(servers, list): servers = []
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                if s.get('pid'): stop_bot_process(s['pid'])
                try: shutil.rmtree(get_server_dir(server_id))
                except: pass
                break
        users[username]['servers'] = [s for s in servers if isinstance(s, dict) and s.get('server_id') != server_id]
        if len(users[username]['servers']) == 0:
            del users[username]
        save_users(users)
    return jsonify({'success': True})

# ============================================
# বট API
# ============================================

@app.route('/api/run/<server_id>', methods=['POST'])
def api_run(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    server, _ = get_server_by_id(server_id)
    if not server: return jsonify({'status': 'error', 'msg': 'Not found'}), 404
    if server.get('status') == 'running': return jsonify({'status': 'error', 'msg': 'Already running!'})
    server['rate_limit_exceeded'] = False
    server['stopped_by_user'] = False
    server['main_file'] = detect_start_file(get_server_dir(server_id))
    server['requirements_file'] = get_requirements_file(get_server_dir(server_id))
    pid, error = run_bot(server_id, server.get('main_file'), server.get('requirements_file'))
    if pid:
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin': continue
            for s2 in data.get('servers', []) if isinstance(data.get('servers', []), list) else []:
                if isinstance(s2, dict) and s2.get('server_id') == server_id:
                    s2['status'] = 'running'; s2['pid'] = pid; s2['started_at'] = str(datetime.now())
                    save_users(users)
                    break
        threading.Thread(target=monitor_bot, args=(server_id, pid), daemon=True).start()
        return jsonify({'status': 'success', 'msg': 'Started!'})
    return jsonify({'status': 'error', 'msg': error or 'Failed'}), 400

@app.route('/api/stop/<server_id>', methods=['POST'])
def api_stop(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    server, _ = get_server_by_id(server_id)
    if not server: return jsonify({'status': 'error', 'msg': 'Not found'}), 404
    if server.get('pid'): stop_bot_process(server['pid'])
    users = load_users()
    for uname, data in users.items():
        if uname == 'admin': continue
        for s2 in data.get('servers', []) if isinstance(data.get('servers', []), list) else []:
            if isinstance(s2, dict) and s2.get('server_id') == server_id:
                s2['status'] = 'stopped'; s2['pid'] = None; s2['stopped_by_user'] = True
                save_users(users); break
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\n[{datetime.now().strftime('%I:%M:%S %p')}] Server stopped by user\n")
    except OSError: pass
    return jsonify({'status': 'success', 'msg': 'Stopped'})

@app.route('/api/logs/<server_id>')
def api_logs(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        with open(log_file, 'r', encoding='utf-8') as f: logs = f.read()
    except FileNotFoundError:
        logs = ''
    except OSError:
        logs = ''
    return jsonify({'logs': logs})

@app.route('/api/clear_logs/<server_id>', methods=['POST'])
def api_clear_logs(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        if os.path.exists(log_file): os.remove(log_file)
        return jsonify({'status': 'success', 'msg': 'Cleared'})
    except OSError as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/command', methods=['POST'])
def api_command():
    data = request.get_json(silent=True) or {}
    cmd = str(data.get('cmd', '')).strip()
    server_id = str(data.get('server_id', '')).strip()
    if not server_id or not require_server_access(server_id): return json_unauthorized()
    if not cmd: return jsonify({'status': 'error', 'msg': 'Command is required'}), 400
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=get_server_dir(server_id), timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        output = (result.stdout + result.stderr)[:2000]
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] $ {cmd}\n{output}\n")
        return jsonify({'status': 'success', 'output': output})
    except: return jsonify({'status': 'error', 'msg': 'Timeout'})

@app.route('/api/stats/<server_id>')
def api_stats(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    server, _ = get_server_by_id(server_id)
    if not server:
        return jsonify({'cpu': '0%', 'ram': '0 MB', 'uptime': '0h', 'status': 'unknown', 'cpu_limit': 80, 'net_in': '0 KB', 'net_out': '0 KB'})
    
    uptime, cpu, ram, net_in, net_out = "0h 0m", "0%", "0 MB", "0 KB", "0 KB"
    
    if server.get('status') == 'running' and server.get('pid'):
        stats = get_process_stats(server['pid'])
        cpu = f"{stats['cpu_percent']}%"
        ram = stats['ram_display']
        net_in, net_out = get_network_stats(server['pid'])
    
    if server.get('status') == 'running' and server.get('started_at'):
        try:
            start = datetime.strptime(server['started_at'], '%Y-%m-%d %H:%M:%S.%f')
            diff = datetime.now() - start
            if diff.days > 0: uptime = f"{diff.days}d {diff.seconds//3600}h"
            else:
                h, m, s = diff.seconds // 3600, (diff.seconds % 3600) // 60, diff.seconds % 60
                uptime = f"{h}h {m}m {s}s"
        except: pass
    
    return jsonify({'cpu': cpu, 'ram': ram, 'uptime': uptime, 'net_in': net_in, 'net_out': net_out, 'cpu_limit': server.get('cpu_limit', 80), 'status': server.get('status', 'stopped')})

# ============================================
# পাসওয়ার্ড চেঞ্জ
# ============================================

@app.route('/api/change_password/<server_id>', methods=['POST'])
def api_change_password(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    data = request.get_json()
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')
    
    if not current_password or not new_password: return jsonify({'error': 'All fields are required!'})
    if len(new_password) < 4: return jsonify({'error': 'Password must be at least 4 characters!'})
    
    users = load_users()
    username = session.get('user')
    
    if username in users:
        if users[username].get('password') == current_password:
            users[username]['password'] = new_password
            save_users(users)
            return jsonify({'success': True, 'msg': 'Password changed!'})
        return jsonify({'error': 'Current password is incorrect!'})
    return jsonify({'error': 'User not found!'}), 404

# ============================================
# External repository deployment is intentionally disabled.
# ============================================

@app.route('/api/files/<server_id>')
def api_files(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    folder = request.args.get('folder', '')
    server_dir = get_server_dir(server_id)
    if folder:
        try:
            server_dir = safe_server_path(server_id, folder)
        except ValueError:
            return jsonify({'files': []})
    if not os.path.exists(server_dir): return jsonify({'files': []})
    
    files = []
    try:
        for item in os.listdir(server_dir):
            item_path = os.path.join(server_dir, item)
            files.append({'name': item, 'is_dir': os.path.isdir(item_path), 'size': os.path.getsize(item_path) if os.path.isfile(item_path) else 0, 'modified': datetime.fromtimestamp(os.path.getmtime(item_path)).strftime('%Y-%m-%d %H:%M')})
    except: pass
    return jsonify({'files': files})

@app.route('/api/file/<server_id>', methods=['GET'])
def api_get_file(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    filename = request.args.get('filename', '')
    try:
        filepath = safe_server_path(server_id, filename)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    if os.path.exists(filepath) and os.path.isfile(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f: return jsonify({'content': f.read()})
        except UnicodeDecodeError:
            return jsonify({'error': 'This file is binary and cannot be edited as text.'}), 400
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/file/<server_id>', methods=['POST'])
def api_save_file(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    data = request.get_json(silent=True) or {}
    try:
        filepath = safe_server_path(server_id, data.get('filename', ''))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    if not data.get('filename'):
        return jsonify({'error': 'Filename is required'}), 400
    content = data.get('content', '')
    if not isinstance(content, str): return jsonify({'error': 'Content must be text'}), 400
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f: f.write(content)
    return jsonify({'success': True})

@app.route('/api/file/<server_id>', methods=['DELETE'])
def api_delete_file(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    data = request.get_json(silent=True) or {}
    try:
        filepath = safe_server_path(server_id, data.get('filename', ''))
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    root = os.path.abspath(get_server_dir(server_id))
    if filepath == root: return jsonify({'error': 'Cannot delete server root'}), 400
    if os.path.exists(filepath):
        if os.path.isdir(filepath): shutil.rmtree(filepath)
        else: os.remove(filepath)
    return jsonify({'success': True})

def safe_server_path(server_id, relative_path):
    root = os.path.abspath(get_server_dir(server_id))
    relative_path = (relative_path or '').replace('\\\\', '/').replace('\\', '/').lstrip('/')
    target = os.path.abspath(os.path.join(root, relative_path))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError('Invalid path')
    return target

def safe_extract_zip(zip_path, destination):
    destination = os.path.abspath(destination)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for member in zf.infolist():
            name = member.filename.replace('\\', '/')
            if not name or name.endswith('/'):
                continue
            # Reject absolute paths and ../ traversal.
            if name.startswith('/') or re.match(r'^[A-Za-z]:', name):
                raise ValueError('Unsafe ZIP path')
            target = os.path.abspath(os.path.join(destination, name))
            if target != destination and not target.startswith(destination + os.sep):
                raise ValueError('Unsafe ZIP path')
        zf.extractall(destination)

@app.route('/api/upload/<server_id>', methods=['POST'])
def api_upload(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    if 'file' not in request.files:
        return jsonify({'error': 'No file selected'}), 400

    folder = request.form.get('folder', '')
    try:
        target_dir = safe_server_path(server_id, folder)
    except ValueError:
        return jsonify({'error': 'Invalid folder path'}), 400

    os.makedirs(target_dir, exist_ok=True)
    uploaded = request.files['file']
    filename = os.path.basename(uploaded.filename or '').strip()
    if not filename:
        return jsonify({'error': 'Invalid filename'}), 400

    target = os.path.join(target_dir, filename)
    uploaded.save(target)

    if filename.lower().endswith('.zip'):
        try:
            safe_extract_zip(target, target_dir)
            return jsonify({
                'success': True,
                'type': 'zip',
                'message': f'{filename} uploaded and extracted successfully.'
            })
        except zipfile.BadZipFile:
            return jsonify({
                'success': False,
                'type': 'zip',
                'error': 'The uploaded ZIP file is invalid or corrupted.'
            }), 400
        except Exception as e:
            return jsonify({
                'success': False,
                'type': 'zip',
                'error': f'ZIP extraction failed: {str(e)}'
            }), 400

    return jsonify({
        'success': True,
        'type': 'file',
        'message': f'{filename} uploaded successfully.'
    })

@app.route('/api/create_folder/<server_id>', methods=['POST'])
def api_create_folder(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    data = request.get_json(silent=True) or {}
    name = str(data.get('foldername', '')).strip()
    if not name: return jsonify({'error': 'Folder name is required'}), 400
    try:
        folder_path = safe_server_path(server_id, name)
    except ValueError:
        return jsonify({'error': 'Invalid folder path'}), 400
    if folder_path == os.path.abspath(get_server_dir(server_id)):
        return jsonify({'error': 'Folder name is required'}), 400
    os.makedirs(folder_path, exist_ok=True)
    return jsonify({'success': True})

@app.route('/api/rename/<server_id>', methods=['POST'])
def api_rename(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    d = request.get_json(silent=True) or {}
    old_name = str(d.get('old_name', '')).strip()
    new_name = str(d.get('new_name', '')).strip()
    if not old_name or not new_name: return jsonify({'error': 'Both old and new names are required'}), 400
    try:
        old_path = safe_server_path(server_id, old_name)
        new_path = safe_server_path(server_id, new_name)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    root = os.path.abspath(get_server_dir(server_id))
    if old_path == root or new_path == root: return jsonify({'error': 'Invalid file path'}), 400
    if not os.path.exists(old_path): return jsonify({'error': 'Not found'}), 404
    if os.path.exists(new_path): return jsonify({'error': 'Destination already exists'}), 409
    os.makedirs(os.path.dirname(new_path), exist_ok=True)
    os.rename(old_path, new_path)
    return jsonify({'success': True})

@app.route('/api/unzip/<server_id>', methods=['POST'])
def api_unzip(server_id):
    if not require_server_access(server_id): return json_unauthorized()
    data = request.get_json(silent=True) or {}
    try:
        zip_path = safe_server_path(server_id, data.get('filename', ''))
    except ValueError:
        return jsonify({'status': 'error', 'msg': 'Invalid path'}), 400
    if not os.path.isfile(zip_path) or not zip_path.lower().endswith('.zip'):
        return jsonify({'status': 'error', 'msg': 'Invalid zip'}), 400
    try:
        safe_extract_zip(zip_path, os.path.dirname(zip_path))
        return jsonify({'status': 'success', 'msg': 'Extracted successfully!'})
    except zipfile.BadZipFile:
        return jsonify({'status': 'error', 'msg': 'Invalid or corrupted ZIP file'}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 400

# ============================================
# স্টার্ট
# ============================================

if __name__ == '__main__':
    print("\n" + "=" * 50)
    print("🚀 DEV HOSTING - FINAL")
    print("=" * 50)
    print("📍 Landing: http://localhost:5000")
    print("📍 Admin: http://localhost:5000/login")
    print("🔗 API: http://localhost:5000/api/create")
    print(f"👤 {ADMIN_USERNAME} / {ADMIN_PASSWORD}")
    print("=" * 50 + "\n")
    app.run(debug=True, host='0.0.0.0', port=5000)