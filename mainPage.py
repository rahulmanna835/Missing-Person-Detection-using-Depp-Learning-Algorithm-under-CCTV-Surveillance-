import os
import io
import re
import cv2
import time
import uuid
import base64
import random
import string
import smtplib
import threading
import subprocess
from functools import wraps
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file
from flask_cors import CORS
from flask_mysqldb import MySQL
from werkzeug.security import generate_password_hash, check_password_hash
import MySQLdb.cursors

import numpy as np
import torch
from ultralytics import YOLO
from facenet_pytorch import MTCNN, InceptionResnetV1
import psutil
import requests
from PIL import Image, ImageDraw

# ==================== CONFIGURATION ====================

app = Flask(__name__)
app.secret_key = 'asdlkf@#$%jhgfd!!jksdhf!@#$%^&*()'

# MySQL Configuration
app.config['MYSQL_HOST'] = 'localhost'
app.config['MYSQL_USER'] = 'root'              # ← Change if needed
app.config['MYSQL_PASSWORD'] = 'GreenBen#123'  # ← Add your MySQL password
app.config['MYSQL_DB'] = 'auth_system'         # ← Database name

mysql = MySQL(app)

# Email Configuration
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "workproject792@gmail.com"           # ← CHANGE THIS
SENDER_PASSWORD = "dmzyurixurqpuumr"   # ← CHANGE THIS

# ---- AI Detection storage / dirs ----
UPLOAD_DIR = "uploads"
RESULT_DIR = "results"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)

reference_store = {}   # image_id -> {path, encoding, filename, width, height}
video_store = {}        # video_id -> {path, fps, frame_count, width, height, duration, filename, location}
jobs = {}                # job_id -> job state (pipeline, logs, stats, timeline, result)

SYSTEM_STATUS_IDLE = {
    "yolo": "Not Loaded",
    "facenet": "Not Loaded",
    "cuda": "Checking...",
    "opencv": "Idle",
    "tensorflow": "Idle",
}

# ==================== Telegram Alert ====================

BOT_TOKEN = "8671393878:AAHYsbi-cCCS2WJhDytJHc8DuDTHx-7WoaY"
CHAT_ID = "6840597467"

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    data = {
        "chat_id": CHAT_ID,
        "text": message
    }

    requests.post(url, data=data)

def send_telegram_photo(image_path, caption="Match Found"):

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    with open(image_path, "rb") as photo:

        requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "caption": caption
            },
            files={
                "photo": photo
            }
        )

# =============================================================================
# AI MODEL LOADING (happens once at startup, not per-request)
# =============================================================================
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

print(f"Loading models on device: {device} ...")
yolo_model = YOLO('yolov8x.pt')
mtcnn = MTCNN(keep_all=True, device=device, post_process=True)
facenet_model = InceptionResnetV1(pretrained='vggface2').eval().to(device)
print("Models loaded.")

HAS_GPU = torch.cuda.is_available()


# =============================================================================
# AUTH HELPERS
# =============================================================================

def generate_otp():
    """Generate 6-digit OTP"""
    return ''.join(random.choices(string.digits, k=6))


def generate_captcha():
    """Generate 6-character CAPTCHA"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


def send_otp_email(email, otp):
    """Send OTP to email"""
    try:
        msg = MIMEMultipart()
        msg['Subject'] = 'Your OTP for Account Verification'
        msg['From'] = "SENDER_EMAIL"
        msg['To'] = email

        html = f"""
        <html>
          <body style="font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px;">
            <div style="background-color: white; padding: 30px; border-radius: 10px; max-width: 400px; margin: 0 auto;">
              <h2 style="color: #333; text-align: center;">Account Verification for Lost Person Detection</h2>
              <p style="color: #666; text-align: center;">Your one-time password is:</p>
              <div style="background-color: #f0f0f0; padding: 20px; border-radius: 8px; text-align: center; margin: 20px 0;">
                <span style="font-size: 32px; font-weight: bold; color: #2563eb; letter-spacing: 5px;">{otp}</span>
              </div>
              <p style="color: #999; text-align: center; font-size: 12px;">This OTP will expire in 10 minutes.</p>
              <p style="color: #999; text-align: center; font-size: 12px;">If you didn't request this, please ignore this email.</p>
            </div>
          </body>
        </html>
        """

        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)

        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False


def is_valid_email(email):
    """Validate email format"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email)


def check_password_strength(password):
    """Check password requirements"""
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not re.search(r"[a-z]", password):
        return False, "Password must contain lowercase letters"
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain uppercase letters"
    if not re.search(r"\d", password):
        return False, "Password must contain numbers"
    return True, ""


def login_required(f):
    """Decorator to check if user is logged in"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def init_db():
    """Initialize database and tables"""
    cursor = mysql.connection.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INT PRIMARY KEY AUTO_INCREMENT,
        email VARCHAR(120) UNIQUE NOT NULL,
        password VARCHAR(255) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS otp_verification (
        id INT PRIMARY KEY AUTO_INCREMENT,
        email VARCHAR(120) NOT NULL,
        otp VARCHAR(6) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at DATETIME NOT NULL,
        is_verified BOOLEAN DEFAULT 0
    )
    """)

    mysql.connection.commit()
    cursor.close()
    print("✅ Auth database initialized successfully")


# =============================================================================
# AI DETECTION HELPERS
# =============================================================================
def get_face_embeddings(pil_image):
    """Runs MTCNN (face detection) + FaceNet (embedding) on a PIL image."""
    boxes, probs = mtcnn.detect(pil_image)
    if boxes is None:
        return [], []
    faces = mtcnn.extract(pil_image, boxes, save_path=None)
    if faces is None:
        return [], []
    with torch.no_grad():
        embeddings = facenet_model(faces.to(device)).cpu().numpy()
    return boxes, embeddings

def get_average_embedding(embeddings_list):
    if not embeddings_list:
        return None
    stacked = np.stack(embeddings_list, axis=0)   # shape: (N, 512)
    mean_embedding = np.mean(stacked, axis=0)      # shape: (512,) — simple average across all photos
    norm = np.linalg.norm(mean_embedding)
    if norm > 0:
        mean_embedding = mean_embedding / norm     # re-normalize so cosine_similarity stays well-behaved
    return mean_embedding


def cosine_similarity(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


def now_str():
    return datetime.now().strftime("%H:%M:%S")


def log(job, text, level="info"):
    job["logs"].append({"time": now_str(), "text": text, "level": level})


def timeline_event(job, label, bold=False):
    job["timeline"].append({"time": now_str(), "label": label, "bold": bold})


def get_system_location():
    try:
        r = requests.get("https://ipapi.co/json/", timeout=4)
        d = r.json()
        city = d.get("city", "")
        country = d.get("country_name", "")
        if city or country:
            return f"{city}, {country}".strip(", ")
    except Exception:
        pass
    return "Location unavailable"


def extract_video_gps(path):
    try:
        result = subprocess.run(
            ["exiftool", "-GPSLatitude", "-GPSLongitude", "-GPSPosition", path],
            capture_output=True, text=True, timeout=5
        )
        out = result.stdout.strip()
        if out:
            return out.replace("\n", " | ")
    except Exception:
        pass
    return None


def get_gpu_usage_percent():
    if HAS_GPU:
        try:
            return round(torch.cuda.utilization(), 1)
        except Exception:
            pass
    return psutil.cpu_percent()


# =============================================================================
# AUTH ROUTES
# =============================================================================

@app.route('/')
def index():
    """Home page - redirect based on login status"""
    if 'user_id' in session:
        return redirect(url_for('dashboard')) # main page where detection is done(dashboard.html)
    return render_template('index.html') # Entry page of the system

@app.route('/users/user1')
def user1Page():
    return render_template('users/user1.html')

@app.route('/users/user2')
def user2Page():
    return render_template('users/user2.html')

@app.route('/users/user3')
def user3Page():
    return render_template('users/user3.html')

@app.route('/users/user4')
def user4Page():
    return render_template('users/user4.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'GET':
        captcha = generate_captcha()
        session['login_captcha'] = captcha
        return render_template('login.html', captcha=captcha)

    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    captcha_input = request.form.get('captcha', '').upper()

    if captcha_input != session.get('login_captcha', '').upper():
        flash('Invalid CAPTCHA', 'danger')
        captcha = generate_captcha()
        session['login_captcha'] = captcha
        return render_template('login.html', email=email, captcha=captcha), 400

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
    user = cursor.fetchone()
    cursor.close()

    if not user:
        flash('Email not registered. Please sign up.', 'warning')
        captcha = generate_captcha()
        session['login_captcha'] = captcha
        return render_template('login.html', email=email, captcha=captcha), 401

    if not check_password_hash(user['password'], password):
        flash('Invalid password', 'danger')
        captcha = generate_captcha()
        session['login_captcha'] = captcha
        return render_template('login.html', email=email, captcha=captcha), 401

    otp = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    cursor = mysql.connection.cursor()
    cursor.execute("DELETE FROM otp_verification WHERE email = %s", (email,))
    cursor.execute(
        "INSERT INTO otp_verification (email, otp, expires_at) VALUES (%s, %s, %s)",
        (email, otp, expires_at)
    )
    mysql.connection.commit()
    cursor.close()

    if send_otp_email(email, otp):
        flash('OTP sent to your email', 'success')
        return redirect(url_for('verify_otp', email=email))
    else:
        flash('Error sending OTP. Please try again.', 'danger')
        captcha = generate_captcha()
        session['login_captcha'] = captcha
        return render_template('login.html', email=email, captcha=captcha), 500


@app.route('/verify-otp')
def verify_otp():
    """OTP verification page"""
    email = request.args.get('email', '').strip().lower()

    if not email:
        return redirect(url_for('login'))

    return render_template('verify_otp.html', email=email)


@app.route('/verify-otp', methods=['POST'])
def verify_otp_submit():
    """Verify OTP and login user"""
    email = request.form.get('email', '').strip().lower()
    otp = request.form.get('otp', '')

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        "SELECT * FROM otp_verification WHERE email = %s AND otp = %s",
        (email, otp)
    )
    otp_record = cursor.fetchone()

    if not otp_record:
        flash('Invalid OTP', 'danger')
        return render_template('verify_otp.html', email=email), 400

    expiry = otp_record['expires_at']
    if isinstance(expiry, str):
        expiry = datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S')

    if expiry < datetime.utcnow():
        flash('OTP expired', 'danger')
        cursor.close()
        return render_template('verify_otp.html', email=email), 400

    cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
    user = cursor.fetchone()

    if not user:
        flash('User not found', 'danger')
        cursor.close()
        return redirect(url_for('login'))

    cursor.execute("DELETE FROM otp_verification WHERE email = %s", (email,))
    mysql.connection.commit()
    cursor.close()

    session['user_id'] = user['id']
    session['email'] = user['email']

    flash('Login successful!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """Signup page"""
    if request.method == 'GET':
        captcha = generate_captcha()
        session['signup_captcha'] = captcha
        return render_template('signup.html', captcha=captcha)

    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')
    captcha_input = request.form.get('captcha', '').upper()

    if captcha_input != session.get('signup_captcha', '').upper():
        flash('Invalid CAPTCHA', 'danger')
        captcha = generate_captcha()
        session['signup_captcha'] = captcha
        return render_template('signup.html', email=email, captcha=captcha), 400

    if not is_valid_email(email):
        flash('Invalid email address', 'danger')
        captcha = generate_captcha()
        session['signup_captcha'] = captcha
        return render_template('signup.html', email=email, captcha=captcha), 400

    is_strong, msg = check_password_strength(password)
    if not is_strong:
        flash(msg, 'danger')
        captcha = generate_captcha()
        session['signup_captcha'] = captcha
        return render_template('signup.html', email=email, captcha=captcha), 400

    if password != confirm_password:
        flash('Passwords do not match', 'danger')
        captcha = generate_captcha()
        session['signup_captcha'] = captcha
        return render_template('signup.html', email=email, captcha=captcha), 400

    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
    existing_user = cursor.fetchone()

    if existing_user:
        flash('Email already registered. Please login.', 'warning')
        cursor.close()
        return redirect(url_for('login'))

    otp = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=10)

    cursor.execute("DELETE FROM otp_verification WHERE email = %s", (email,))
    cursor.execute(
        "INSERT INTO otp_verification (email, otp, expires_at) VALUES (%s, %s, %s)",
        (email, otp, expires_at)
    )
    mysql.connection.commit()
    cursor.close()

    if send_otp_email(email, otp):
        session['signup_email'] = email
        session['signup_password'] = password
        flash('OTP sent to your email', 'success')
        return redirect(url_for('signup_verify_otp'))
    else:
        flash('Error sending OTP. Please try again.', 'danger')
        captcha = generate_captcha()
        session['signup_captcha'] = captcha
        return render_template('signup.html', email=email, captcha=captcha), 500


@app.route('/signup/verify-otp', methods=['GET', 'POST'])
def signup_verify_otp():
    """OTP verification page for signup"""
    email = session.get('signup_email')

    if not email:
        flash('Please signup first', 'warning')
        return redirect(url_for('signup'))

    if request.method == 'GET':
        return render_template('signup_verify_otp.html', email=email)

    otp = request.form.get('otp', '')

    cursor = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute(
        "SELECT * FROM otp_verification WHERE email = %s AND otp = %s",
        (email, otp)
    )
    otp_record = cursor.fetchone()

    if not otp_record:
        flash('Invalid OTP', 'danger')
        return render_template('signup_verify_otp.html', email=email), 400

    expiry = otp_record['expires_at']
    if isinstance(expiry, str):
        expiry = datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S')

    if expiry < datetime.utcnow():
        flash('OTP expired', 'danger')
        cursor.close()
        return render_template('signup_verify_otp.html', email=email), 400

    password = session.get('signup_password')
    hashed_password = generate_password_hash(password)

    try:
        cursor.execute(
            "INSERT INTO users (email, password) VALUES (%s, %s)",
            (email, hashed_password)
        )

        cursor.execute("DELETE FROM otp_verification WHERE email = %s", (email,))
        mysql.connection.commit()

        cursor.execute("SELECT id, email FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()

        session['user_id'] = user['id']
        session['email'] = user['email']

        session.pop('signup_email', None)
        session.pop('signup_password', None)

        flash('Account created successfully! Welcome!', 'success')
        return redirect(url_for('dashboard'))

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(e)
        flash('Error creating account. Please try again.', 'danger')
        return render_template('signup_verify_otp.html', email=email), 500
    finally:
        cursor.close()


@app.route('/dashboard')
@login_required
def dashboard():
    if 'email' not in session:
        flash("Session expired. Please login again.", "warning")
        return redirect(url_for('login'))

    return render_template('dashboard.html', email=session['email'])


@app.route('/logout')
def logout():
    """Logout user"""
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('index'))


@app.route('/api/refresh-captcha', methods=['POST'])
def refresh_captcha():
    """Refresh CAPTCHA"""
    page = request.json.get('page', 'login')
    captcha = generate_captcha()

    if page == 'login':
        session['login_captcha'] = captcha
    else:
        session['signup_captcha'] = captcha

    return jsonify({'captcha': captcha})


# =============================================================================
# AI DETECTION ROUTES  (all under /api/... so they don't clash with auth routes)
# =============================================================================

@app.route("/api/upload-reference", methods=["POST"])
def upload_reference():
    try:
        files = request.files.getlist("reference_images")

        if not files:
            return jsonify({"error": "No files"}), 400

        results = []

        for f in files:
            img_id = uuid.uuid4().hex[:8]
            path = os.path.join(UPLOAD_DIR, f.filename)
            f.save(path)

            image = Image.open(path).convert("RGB")

            boxes, embeddings = get_face_embeddings(image)

            encoding = embeddings[0] if len(embeddings) > 0 else None

            reference_store[img_id] = {
                "path": path,
                "encoding": encoding,
                "filename": f.filename,
                "width": image.width,
                "height": image.height,
            }

            results.append({
                "id": img_id,
                "filename": f.filename,
                "width": image.width,
                "height": image.height,
                "face_detected": len(boxes) > 0,
                "encoding_ready": encoding is not None
            })

        return jsonify({"images": results})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload-video", methods=["POST"])
def upload_video():
    if "surveillance_video" not in request.files:
        return jsonify({"error": "No file received (expected field 'surveillance_video')"}), 400

    f = request.files["surveillance_video"]
    video_id = uuid.uuid4().hex[:8]
    path = os.path.join(UPLOAD_DIR, f"{video_id}_{f.filename}")
    f.save(path)

    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = frame_count / fps if fps else 0

    mid_frame_index = frame_count // 2 if frame_count else 0
    cap.set(cv2.CAP_PROP_POS_FRAMES, mid_frame_index)
    ok, mid_frame = cap.read()
    thumb_b64 = ""
    if ok:
        _, buf = cv2.imencode(".jpg", mid_frame)
        thumb_b64 = base64.b64encode(buf).decode()
    cap.release()

    location = extract_video_gps(path) or get_system_location()

    video_store[video_id] = {
        "path": path, "fps": fps, "frame_count": frame_count,
        "width": width, "height": height, "duration": duration,
        "filename": f.filename, "location": location,
    }

    mins, secs = divmod(int(duration), 60)
    return jsonify({
        "id": video_id,
        "filename": f.filename,
        "duration": f"00:{mins:02d}:{secs:02d}",
        "resolution": f"{width}x{height}",
        "frames_extracted": frame_count,
        "location": location,
        "thumbnail_b64": thumb_b64,
    })


@app.route("/api/start-detection", methods=["POST"])
def start_detection():
    data = request.get_json(force=True) or {}
    reference_ids = data.get("reference_ids", [])
    video_id = data.get("video_id")

    if not reference_ids:
        return jsonify({"error": "No reference image ids provided"}), 400
    if video_id not in video_store:
        return jsonify({"error": "Unknown video_id — upload a video first"}), 400

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "pipeline": {
            "image_uploaded": True,
            "face_encoding": False,
            "processing_video": False,
            "extracting_frames": False,
            "comparing_features": False,
            "calculating_similarity": False,
            "generating_result": False,
        },
        "logs": [],
        "timeline": [],
        "stats": {
            "frames_processed": 0,
            "faces_detected": 0,
            "inference_time_ms": 0,
            "gpu_usage": 0,
            "ram_usage_gb": 0,
            "match_confidence": 0,
            "processing_fps": 0,
        },
        "system_status": dict(SYSTEM_STATUS_IDLE),
        "done": False,
        "result": None,
    }

    thread = threading.Thread(target=run_detection_job, args=(job_id, reference_ids, video_id), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


def run_detection_job(job_id, reference_ids, video_id, match_threshold=90.0):
    job = jobs[job_id]
    start_time = time.time()

    log(job, "Initializing AI Engine...")
    job["system_status"]["opencv"] = "Running"
    time.sleep(0.2)

    log(job, "YOLO person detector ready...")
    job["system_status"]["yolo"] = "Loaded"
    time.sleep(0.15)

    log(job, "MTCNN + FaceNet (InceptionResnetV1) face embedding model ready...")
    job["system_status"]["facenet"] = "Loaded"
    job["system_status"]["cuda"] = "Available" if HAS_GPU else "CPU Mode (no CUDA GPU found)"
    job["system_status"]["tensorflow"] = "Running"

    ref_encodings = []
    for rid in reference_ids:
        entry = reference_store.get(rid)
        if entry and entry.get("encoding") is not None:
            ref_encodings.append(entry["encoding"])

    if not ref_encodings:
        log(job, "No usable face encodings found in reference image(s)!", "warn")
        job["done"] = True
        return

    log(job, f"Extracted {len(ref_encodings)} face encoding(s) from reference image(s)", "ok")
    average_ref_embedding = get_average_embedding(ref_encodings)
    log(job, f"Averaged {len(ref_encodings)} reference photo(s) into a single identity embedding", "ok")

    job["pipeline"]["face_encoding"] = True
    timeline_event(job, "Face Encoding Generated")

    video = video_store[video_id]
    log(job, f"Reading video: {video['filename']}")
    job["pipeline"]["processing_video"] = True
    timeline_event(job, "Video Processing Started")

    cap = cv2.VideoCapture(video["path"])
    total_frames = video["frame_count"]
    log(job, f"Total frames found: {total_frames}")

    sample_every = max(1, int(video["fps"] // 5)) if video["fps"] else 5
    job["pipeline"]["extracting_frames"] = True
    log(job, f"Sampling every {sample_every} frame(s) for face detection...")
    timeline_event(job, "Extracting Frames")

    best_match = None
    frame_idx = 0
    job["pipeline"]["comparing_features"] = True
    log(job, "Matching faces against reference encoding(s)...")
    timeline_event(job, "Face Matching In Progress")

    # precompute the gamma-correction lookup table once (perf win vs rebuilding per-frame)
    gamma = 1.2
    inv_gamma = 1.0 / gamma
    gamma_table = np.array([
        ((i / 255.0) ** inv_gamma) * 255 for i in np.arange(256)
    ]).astype("uint8")

    sharpen_kernel = np.array([
        [0, -1, 0],
        [-1, 5, -1],
        [0, -1, 0]
    ])

    stop_early = False

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % sample_every == 0:
            t0 = time.time()

            yolo_results = yolo_model(frame, classes=[0], verbose=False)
            person_boxes = yolo_results[0].boxes if len(yolo_results) else []

            for box in person_boxes:
                conf = float(box.conf[0])
                if conf < 0.5:
                    continue

                x1, y1, x2, y2 = map(int, box.xyxy[0])

                # padding, like doc 4
                padding = 25
                x1 = max(0, x1 - padding)
                y1 = max(0, y1 - padding)
                x2 = min(frame.shape[1], x2 + padding)
                y2 = min(frame.shape[0], y2 + padding)

                person_crop = frame[y1:y2, x1:x2]
                ch, cw = person_crop.shape[:2]
                if ch < 80 or cw < 80:
                    continue  # skip tiny/low-quality crops

                # image enhancement: gamma correction + sharpening
                person_crop = cv2.LUT(person_crop, gamma_table)
                person_crop = cv2.filter2D(person_crop, -1, sharpen_kernel)

                rgb = cv2.cvtColor(person_crop, cv2.COLOR_BGR2RGB)
                pil_crop = Image.fromarray(rgb)

                boxes, embeddings = get_face_embeddings(pil_crop)
                if boxes is None or len(embeddings) == 0:
                    continue

                job["stats"]["faces_detected"] += len(embeddings)

                for fbox, emb in zip(boxes, embeddings):
                    sim = cosine_similarity(emb, average_ref_embedding) * 100
                    if best_match is None or sim > best_match["similarity"]:
                        fx1, fy1, fx2, fy2 = fbox
                        left, top = int(fx1) + x1, int(fy1) + y1
                        right, bottom = int(fx2) + x1, int(fy2) + y1
                        best_match = {
                            "similarity": sim,
                            "frame_index": frame_idx,
                            "bbox": (top, right, bottom, left),
                            "frame": frame.copy(),
                        }

                    if sim >= match_threshold:
                        stop_early = True
                        break

                if stop_early:
                    break

            inference_ms = (time.time() - t0) * 1000
            job["stats"]["inference_time_ms"] = round(inference_ms, 1)
            job["stats"]["frames_processed"] += 1
            job["stats"]["ram_usage_gb"] = round(psutil.Process().memory_info().rss / (1024 ** 3), 2)
            job["stats"]["gpu_usage"] = round(get_gpu_usage_percent(), 1)

            elapsed = max(0.001, time.time() - start_time)
            job["stats"]["processing_fps"] = round(job["stats"]["frames_processed"] / elapsed, 1)

        frame_idx += 1
        if stop_early:
            break

    cap.release()
    job["pipeline"]["calculating_similarity"] = True
    timeline_event(job, "Calculating Similarity")

    if best_match is None:
        log(job, "No matching face found in the video.", "warn")
        job["done"] = True
        return

    log(job, f"Match found! Similarity score: {best_match['similarity']:.1f}%", "ok")
    job["stats"]["match_confidence"] = round(best_match["similarity"], 1)

    top, right, bottom, left = best_match["bbox"]
    frame = best_match["frame"]
    h, w, _ = frame.shape
    pad = 20
    crop = frame[max(0, top - pad):min(h, bottom + pad), max(0, left - pad):min(w, right + pad)]
    boxed = frame.copy()
    cv2.rectangle(boxed, (left, top), (right, bottom), (0, 0, 255), 3)

    video_seconds = best_match["frame_index"] / video["fps"] if video["fps"] else 0
    mins, secs = divmod(int(video_seconds), 60)
    result_timestamp = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (video @ {mins:02d}:{secs:02d})"

    _, boxed_buf = cv2.imencode(".jpg", boxed)
    _, crop_buf = cv2.imencode(".jpg", crop)

    result_id = uuid.uuid4().hex[:8]
    boxed_path = os.path.join(RESULT_DIR, f"{result_id}_matched_frame.jpg")

    with open(boxed_path, "wb") as fh:
        fh.write(boxed_buf.tobytes())

    # ---------------------------------------------------------
    # TELEGRAM ALERT
    # ---------------------------------------------------------
    try:
        similarity = round(best_match["similarity"], 2)
        best_similarity = round(round(best_match["similarity"], 1) / 100, 2)
        timestamp = result_timestamp
        image_name = boxed_path

        message = f"""
    🚨 ALERT
    Missing Person Detected

    Best Similarity: {best_similarity:.2f}

    Time: {timestamp}

    Camera: {video["filename"]}

    Location: {"Jaipur, India"}
    """

        send_telegram_message(message)
        send_telegram_photo(image_name)

        log(job, "Telegram alert sent successfully.", "ok")

    except Exception as e:
        log(job, f"Telegram alert failed: {str(e)}", "warn")

    job["pipeline"]["generating_result"] = True
    timeline_event(job, "Result Generated", bold=True)
    log(job, "Target person detected ✅", "ok")

    job["result"] = {
        "matched_frame_b64": base64.b64encode(boxed_buf).decode(),
        "cropped_face_b64": base64.b64encode(crop_buf).decode(),
        "similarity": round(best_match["similarity"], 2),
        "similarity_score": round(round(best_match["similarity"], 1) / 100, 2),
        "timestamp": result_timestamp,
        "location": video.get("location", "Unknown"),
        "camera": video["filename"],
        "boxed_path": boxed_path,
    }
    job["done"] = True































@app.route("/api/job-status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    safe = {k: v for k, v in job.items()}
    if safe.get("result"):
        safe["result"] = {k: v for k, v in safe["result"].items() if k != "boxed_path"}
    return jsonify(safe)


@app.route("/api/download-result/<job_id>")
def download_result(job_id):
    job = jobs.get(job_id)
    if not job or not job.get("result"):
        return jsonify({"error": "no result available for this job"}), 404

    result = job["result"]
    img = Image.open(result["boxed_path"]).convert("RGB")
    draw = ImageDraw.Draw(img)

    caption = f"{result['timestamp']}  |  {result['location']}  |  Similarity: {result['similarity']}%"
    bar_height = 32
    draw.rectangle([(0, img.height - bar_height), (img.width, img.height)], fill=(0, 0, 0))
    draw.text((10, img.height - bar_height + 8), caption, fill=(255, 255, 255))

    out_path = result["boxed_path"].replace(".jpg", "_labeled.jpg")
    img.save(out_path)

    return send_file(out_path, as_attachment=True,
                      download_name=f"detection_result_{job_id}.jpg")


# =============================================================================
# ERROR HANDLERS
# =============================================================================

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500


# =============================================================================
# RUN APP
# =============================================================================

if __name__ == '__main__':
    with app.app_context():
        init_db()

    # NOTE: original auth app ran on port 5000, original AI app ran on port 8000.
    # They're now one app on ONE port. If your frontend HTML has
    # API_BASE = "http://localhost:8000" hardcoded, either change it to match
    # the port below, or change PORT back to 8000.
    PORT = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='localhost', port=PORT)