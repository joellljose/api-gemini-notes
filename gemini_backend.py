import google.generativeai as genai
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import json
import re
import requests
import fitz  
import os
import io
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

app = Flask(__name__, static_folder='static')
CORS(app)

# Robust Path Handling
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / '.env'
CREDS_PATH = BASE_DIR / 'serviceAccountKey.json'

load_dotenv(dotenv_path=ENV_PATH)
print(f"Loading .env from: {ENV_PATH}")

# Ensure upload directory exists
UPLOAD_FOLDER = BASE_DIR / 'static' / 'notes'
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
print(f"Uploads will be saved to: {UPLOAD_FOLDER}")

# --- Firebase Init (For Firestore Only) ---
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(CREDS_PATH))
        firebase_admin.initialize_app(cred)
    print("Firebase Admin Initialized")
except Exception as e:
    print(f"Warning: Firebase Admin not initialized: {e}")
# ---------------------

import random
API_KEYS = os.environ.get("GEMINI_API")
# API_KEYS = [
#     os.environ.get("GEMINI_API_KEY_1"),
#     os.environ.get("GEMINI_API_KEY_2"),
#     os.environ.get("GEMINI_API_KEY_3"),
#     os.environ.get("GEMINI_API_KEY_4"),
#     os.environ.get("GEMINI_API_KEY"), # Fallback for legacy support
# ]
# Filter out None values just in case
API_KEYS = [k for k in API_KEYS if k]

if not API_KEYS:
    print("WARNING: No GEMINI_API_KEYS found in environment variables.")

MODEL_NAME = 'gemini-2.5-flash'

def get_configured_model():
    """
    Selects a random API key, configures GenAI, increments usage stats, 
    and returns the model instance.
    """
    if not API_KEYS:
        raise Exception("No API Keys available.")
    
    selected_key_index = random.randint(0, len(API_KEYS) - 1)
    selected_key = API_KEYS[selected_key_index]
    key_name = f"key_{selected_key_index + 1}"
    
    # Configure GenAI with the selected key
    genai.configure(api_key=selected_key)
    
    # Track usage in Firestore
    try:
        db = firestore.client()
        stats_ref = db.collection('stats').document('api_usage')
        stats_ref.set({key_name: firestore.Increment(1)}, merge=True)
        print(f"Using {key_name} ... Usage tracked.")
    except Exception as e:
        print(f"Error tracking API usage: {e}")
        
    return genai.GenerativeModel(model_name=MODEL_NAME) 

import cloudinary
import cloudinary.uploader

# --- Cloudinary Init ---
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET")

if CLOUDINARY_CLOUD_NAME:
    cloudinary.config( 
        cloud_name = CLOUDINARY_CLOUD_NAME, 
        api_key = CLOUDINARY_API_KEY, 
        api_secret = CLOUDINARY_API_SECRET,
        secure = True
    )
    print("Cloudinary Configured")
else:
    print("WARNING: Cloudinary credentials not found in env")

def upload_to_cloudinary(file_obj, filename):
    try:
        # Sanitize filename: replace spaces with underscores, keep alphanumeric and dots
        clean_filename = "".join([c if c.isalnum() or c in "._-" else "_" for c in filename])
        
        # Determine resource type: 'raw' for PDFs to avoid image transformation strictness, 'auto' otherwise
        res_type = "raw" if filename.lower().endswith(".pdf") else "auto"

        print(f"Uploading {clean_filename} to Cloudinary as {res_type}...")
        response = cloudinary.uploader.upload(
            file_obj, 
            resource_type = res_type,
            folder = "ktu_notes",
            public_id = clean_filename.split('.')[0] 
        )
        
        web_url = response.get("secure_url")
        print(f"File uploaded to Cloudinary: {web_url}")
        return web_url, response.get("public_id")

    except Exception as e:
        print(f"Cloudinary Upload Error: {e}")
        raise e

def extract_text_from_pdf_stream(file_stream):
    try:
        with fitz.open(stream=file_stream.read(), filetype="pdf") as doc:
            text = ""
            for page in doc:
                text += page.get_text()
        file_stream.seek(0) # Reset stream
        return text
    except Exception as e:
        print(f"PDF Extract Error: {e}")
        return ""

@app.route('/verify-note', methods=['POST'])
def verify_note():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
        
        file = request.files['file']
        subject = request.form.get('subject', 'Unknown Subject')
        module = request.form.get('module', 'Unknown Module')
        
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400

        # Save to temp file to avoid RAM OOM on Render
        filename = file.filename
        temp_path = UPLOAD_FOLDER / filename
        file.save(str(temp_path))
        print(f"Saved temp file to: {temp_path}")

        # Check Global Settings for AI Verification
        ai_enabled = True # Default
        try:
            db = firestore.client()
            settings_ref = db.collection('config').document('settings')
            doc = settings_ref.get()
            if doc.exists:
                ai_enabled = doc.to_dict().get('enableAiVerification', True)
        except Exception as db_e:
            print(f"Error fetching settings: {db_e}. Defaulting to AI ON.")

        extracted_text = ""
        status = "pending"
        reason = "AI Verification Failed or Skipped"
        ai_summary = "No summary generated."

        # If AI is disabled, skip extraction and verification
        if not ai_enabled:
             print("AI Verification is DISABLED by Admin. Skipping...")
             status = "pending"
             reason = "Manual Mode Active (AI Verification Disabled)"
             ai_summary = "Pending Admin Review"
        else:
            # AI Verification Logic
            try:
                # Extract text using file path (memory efficient)
                with fitz.open(temp_path) as doc:
                    for page in doc:
                        extracted_text += page.get_text()
                
                if extracted_text.strip():
                    model = get_configured_model()
                    prompt = f"""
                    Act as a Syllabus Validator for KTU Engineering Course.
                    Subject: {subject}
                    Module: {module}
                    
                    Content of the Note:
                    {extracted_text[:10000]}
                    
                    Task:
                    1. Verify if the content is relevant to the Subject and Module provided.
                    2. If relevant, status is "approved". If completely irrelevant (spam, wrong subject), status is "rejected". If unsure or partially correct, status is "pending".
                    3. Generate a short 2-sentence summary of the note.
                    
                    Return ONLY JSON:
                    {{
                        "status": "approved" | "rejected" | "pending",
                        "reason": "Explanation for the decision",
                        "summary": "Short summary of the content"
                    }}
                    """
                    
                    try:
                        response = model.generate_content(prompt)
                        clean_json = re.sub(r'```json|```', '', response.text).strip()
                        ai_data = json.loads(clean_json)
                        status = ai_data.get('status', 'pending')
                        reason = ai_data.get('reason', 'AI Review')
                        ai_summary = ai_data.get('summary', 'Summary not found')
                    except Exception as ai_e:
                        print(f"AI Error: {ai_e}")
                        status = "pending"
                        reason = "AI Processing Error, marked as pending for human review."
            except Exception as e:
                print(f"Extraction Error: {e}")
                # If extraction fails (not a PDF?), we can still upload or fail
                # For now, continue to upload but mark as pending

        # Upload to Cloudinary
        print("Uploading to Cloudinary...")
        # Upload from path
        file_url, file_path_id = upload_to_cloudinary(str(temp_path), filename) # Cloudinary python SDK supports path string
        
        # Clean up temp file
        try:
            if temp_path.exists():
                os.remove(temp_path)
                print(f"Removed temp file: {temp_path}")
        except Exception as cleanup_e:
            print(f"Cleanup Error: {cleanup_e}")

        return jsonify({
            "status": status,
            "reason": reason,
            "summary": ai_summary,
            "url": file_url,
            "fileId": file_path_id
        })

    except Exception as e:
        print(f"Verify Note Error: {e}")
        # Attempt minimal cleanup
        try:
             if 'temp_path' in locals() and temp_path.exists():
                 os.remove(temp_path)
        except: pass
        return jsonify({"error": str(e)}), 500

# Endpoint to extract text from an existing URL (used by quiz generation)
def extract_text_from_url(url):
    headers = {
        'User-Agent': 'Mozilla/5.0'
    }
    try:
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code == 200:
            with fitz.open(stream=response.content, filetype="pdf") as doc:
                text = ""
                for page in doc:
                    text += page.get_text()
            return text
        else:
            raise Exception(f"Download failed with status: {response.status_code}")
    except Exception as e:
        raise Exception(f"Failed to extract text from PDF: {str(e)}")

@app.route('/generate-quiz', methods=['POST'])
def generate_quiz():
    try:
        data = request.get_json()
        input_text = data.get('text', '')
        pdf_url = data.get('url', '')
        
        source_text = ""

        if input_text and input_text.strip():
            print("Generating quiz from provided description/text...")
            source_text = input_text
        elif pdf_url and pdf_url.strip():
            print(f"Generating quiz from PDF URL: {pdf_url}...")
            source_text = extract_text_from_url(pdf_url)
        else:
            return jsonify({"error": "No text or PDF URL provided"}), 400
        
        if not source_text.strip():
             return jsonify({"error": "Extracted text is empty"}), 400
        
        model = genai.GenerativeModel(model_name=MODEL_NAME)
        
        prompt = f"""
        Act as a University Professor. Generate 5 Multiple Choice Questions (MCQs) based on the following text.
        Return ONLY a JSON array of objects.
        
        Each object must have:
        - "question": The question string.
        - "options": A list of 4 distinct options strings.
        - "correctIndex": Integer (0-3) indicating the correct option.
        
        Text content:
        {source_text[:12000]}
        """

        response = model.generate_content(prompt)
        
        # --- Increment Quiz Counter ---
        try:
            db = firestore.client()
            stats_ref = db.collection('stats').document('quiz_generation')
            stats_ref.set({'count': firestore.Increment(1)}, merge=True)
            print("Quiz generation counter incremented.")
        except Exception as db_error:
            print(f"Error updating Firestore stats: {db_error}")
        # ------------------------------
        
        clean_json = re.sub(r'```json|```', '', response.text).strip()
        quiz_data = json.loads(clean_json)
        
        return jsonify(quiz_data)

    except Exception as e:
        print(f"CRITICAL ERROR: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/generate-summary', methods=['POST'])
def generate_summary():
    try:
        data = request.get_json()
        pdf_url = data.get('url', '')
        
        if not pdf_url:
             return jsonify({"error": "No PDF URL provided"}), 400

        print(f"Generating summary for: {pdf_url}...")
        
        # 1. Extract Text
        source_text = extract_text_from_url(pdf_url)
        
        if not source_text.strip():
             return jsonify({"error": "Extracted text is empty"}), 400

        # 2. Call Gemini
        model = get_configured_model()
        prompt = f"""
        Act as an academic expert. 
        Summarize the following text into 3-5 concise, high-value bullet points suitable for quick revision.
        Focus on key concepts, definitions, and formulas.
        
        Text:
        {source_text[:15000]}
        """
        
        response = model.generate_content(prompt)
        summary = response.text.strip()
        
        return jsonify({"summary": summary})

    except Exception as e:
        print(f"Summary Generation Error: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/participatory-start', methods=['POST'])
def participatory_start():
    try:
        data = request.get_json()
        input_text = data.get('text', '')
        
        model = get_configured_model()
        
        prompt = f"""
        You are a Participatory Learning Facilitator for KTU Engineering students.
        Source Material: {input_text[:10000]}
        
        Task:
        1. Concept Challenge: Explain a complex concept but leave out a key detail.
        2. Question Design: Ask student to write a tricky MCQ.
        
        Output Format (JSON Only):
        {{
            "facilitator_intro": "...",
            "challenge": "...",
            "creation_task": "..."
        }}
        """
        
        response = model.generate_content(prompt)
        clean_json = re.sub(r'```json|```', '', response.text).strip()
        return jsonify(json.loads(clean_json))

    except Exception as e:
        print(f"Participatory Start Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/participatory-evaluate', methods=['POST'])
def participatory_evaluate():
    try:
        data = request.get_json()
        original_text = data.get('text', '')
        student_answer = data.get('answer', '')
        student_question = data.get('question', '')
        challenge_context = data.get('challenge', '')

        model = get_configured_model()

        prompt = f"""
        Act as a Facilitator.
        Original: {original_text[:5000]}
        Challenge: {challenge_context}
        Student Answer: {student_answer}
        Student Question: {student_question}
        
        Output (JSON Only):
        {{
            "concept_feedback": "...",
            "question_critique": "...",
            "overall_score": "..."
        }}
        """
        
        response = model.generate_content(prompt)
        clean_json = re.sub(r'```json|```', '', response.text).strip()
        return jsonify(json.loads(clean_json))

    except Exception as e:
        print(f"Participatory Eval Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- Admin Notification Endpoint ---
from firebase_admin import messaging

@app.route('/send-notification', methods=['POST'])
def send_notification():
    try:
        data = request.get_json()
        title = data.get('title', 'Notification')
        body = data.get('body', '')

        if not body:
            return jsonify({"error": "Body is required"}), 400

        # Create message for 'all_users' topic
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            topic='all_users',
        )

        # Send
        response = messaging.send(message)
        print(f"Successfully sent message: {response}")
        
        return jsonify({"success": True, "messageId": response})

    except Exception as e:
        print(f"Notification Error: {e}")
        return jsonify({"error": str(e)}), 500
# --- Telegram Monitoring & Alerts ---
import psutil
import time
import requests
import traceback
import threading
from functools import wraps
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import atexit

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram_alert(message):
    """Sends a message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not set. Skipping alert.")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")

def get_system_status():
    """Returns a formatted string of system stats (CPU, RAM)."""
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    ram_usage = memory.percent
    
    return (
        f"ℹ️ *System Status Report*\n\n"
        f"💻 **CPU Usage:** {cpu_percent}%\n"
        f"🧠 **RAM Usage:** {ram_usage}%\n"
        f"✅ **Backend Status:** Online & Active"
    )

def periodic_status_report():
    """Scheduled job to send status report."""
    status_msg = get_system_status()
    send_telegram_alert(status_msg)

# Initialize Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(
    func=periodic_status_report,
    trigger=IntervalTrigger(minutes=5),
    id='status_report_job',
    name='Send Telegram Status Report every 5 mins',
    replace_existing=True
)
try:
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    print("Telegram Background Scheduler Started.")
    # Send an initial startup message
    send_telegram_alert("🚀 **Backend Started Successfully**\nSystem monitoring is active.")
except Exception as e:
    print(f"Failed to start scheduler: {e}")

# Performance Monitor Decorator
def monitor_performance(threshold_seconds=10):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = f(*args, **kwargs)
                return result
            except Exception as e:
                # Let the global error handler catch exceptions
                raise e
            finally:
                execution_time = time.time() - start_time
                if execution_time > threshold_seconds:
                    endpoint = request.path
                    alert_msg = (
                        f"⚠️ **Slow Performance Alert**\n\n"
                        f"📍 **Endpoint:** `{endpoint}`\n"
                        f"⏱️ **Time Taken:** {execution_time:.2f}s\n"
                        f"⚠️ **Threshold:** {threshold_seconds}s"
                    )
                    # Run alert in background to not block response more
                    threading.Thread(target=send_telegram_alert, args=(alert_msg,)).start()
        return wrapper
    return decorator

# Global Error Handler
@app.errorhandler(Exception)
def handle_global_exception(e):
    # Get traceback
    tb_str = traceback.format_exc()
    endpoint = request.path
    method = request.method
    
    alert_msg = (
        f"🚨 **CRASH ALERT** 🚨\n\n"
        f"📍 **Endpoint:** `{method} {endpoint}`\n"
        f"❌ **Error:** `{str(e)}`\n\n"
        f"📜 **Traceback:**\n```python\n{tb_str[:1500]}...```" # Truncate if too long
    )
    
    # Send alert in background
    threading.Thread(target=send_telegram_alert, args=(alert_msg,)).start()
    
    # Return standard error to client
    print(f"Global Error Caught: {e}")
    return jsonify({"error": "Internal Server Error", "details": str(e)}), 500

# Wrap critical endpoints with performance monitor
app.view_functions['verify_note'] = monitor_performance(threshold_seconds=15)(app.view_functions['verify_note'])
app.view_functions['generate_quiz'] = monitor_performance(threshold_seconds=10)(app.view_functions['generate_quiz'])
app.view_functions['generate_summary'] = monitor_performance(threshold_seconds=10)(app.view_functions['generate_summary'])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
