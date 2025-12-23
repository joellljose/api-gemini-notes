import google.generativeai as genai
from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import re
import requests
import fitz  
import os
import io
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

app = Flask(__name__)
CORS(app)

load_dotenv()

# --- Firebase Init ---
try:
    if not firebase_admin._apps:
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
    print("Firebase Admin Initialized")
except Exception as e:
    print(f"Warning: Firebase Admin not initialized: {e}")
# ---------------------

API_KEY = os.environ.get("GEMINI_API_KEY")

if not API_KEY:
    print("WARNING: GEMINI_API_KEY not found in environment variables.")
genai.configure(api_key=API_KEY)

MODEL_NAME = 'gemini-2.5-flash' 

# --- Google Drive Init ---
SCOPES = ['https://www.googleapis.com/auth/drive.file']

def get_drive_service():
    creds = None
    try:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            'serviceAccountKey.json', scopes=SCOPES)
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"Drive Auth Error: {e}")
        return None

def upload_file_to_drive(file_obj, filename, mime_type='application/pdf'):
    service = get_drive_service()
    if not service:
        raise Exception("Google Drive Service Unreachable")

    file_metadata = {'name': filename}
    
    # Check for specific folder to upload to (Critical for Service Accounts with no quota)
    folder_id = os.environ.get("DRIVE_FOLDER_ID")
    if folder_id:
        file_metadata['parents'] = [folder_id]

    media = MediaIoBaseUpload(file_obj, mimetype=mime_type, resumable=True)
    
    file = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink, webContentLink').execute()
    
    # Make it readable by anyone with the link
    try:
        permission = {'type': 'anyone', 'role': 'reader'}
        service.permissions().create(fileId=file.get('id'), body=permission).execute()
    except Exception as e:
        print(f"Permission Error: {e}")

    return file.get('webViewLink'), file.get('id')

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

        file_content = file.read()
        file_stream = io.BytesIO(file_content)

        # AI Verification
        extracted_text = extract_text_from_pdf_stream(io.BytesIO(file_content))
        status = "pending"
        reason = "AI Verification Failed or Skipped"
        ai_summary = "No summary generated."
        
        if extracted_text.strip():
            model = genai.GenerativeModel(model_name=MODEL_NAME)
            prompt = f"""
            Act as a Syllabus Validator for an Engineering Course.
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

        # Upload to Drive
        drive_link, file_id = upload_file_to_drive(io.BytesIO(file_content), file.filename)
        
        return jsonify({
            "status": status,
            "reason": reason,
            "summary": ai_summary,
            "url": drive_link,
            "fileId": file_id
        })

    except Exception as e:
        print(f"Verify Note Error: {e}")
        return jsonify({"error": str(e)}), 500

# Endpoint to extract text from an existing Drive URL (used by quiz generation)
def extract_text_from_drive(url):
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
            source_text = extract_text_from_drive(pdf_url)
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

@app.route('/participatory-start', methods=['POST'])
def participatory_start():
    try:
        data = request.get_json()
        input_text = data.get('text', '')
        
        model = genai.GenerativeModel(model_name=MODEL_NAME)
        
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

        model = genai.GenerativeModel(model_name=MODEL_NAME)

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
