# ===================== app.py =====================

import os
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from flask_caching import Cache
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer
import pdfplumber
from google import genai

# ================= LOAD ENV =================
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ================= GEMINI API =================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
USE_AI = False
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    USE_AI = True

# ================= CONFIG =================
app = Flask(__name__)

# Secret key setup (fixed)
SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY") or "dev-fallback-key"
app.secret_key = SECRET_KEY
serializer = URLSafeTimedSerializer(SECRET_KEY)

# Database
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///database.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Mail configuration
app.config.update(
    MAIL_SERVER='smtp.gmail.com',
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
    MAIL_USERNAME=os.getenv('MAIL_USERNAME'),
    MAIL_PASSWORD=os.getenv('MAIL_PASSWORD')
)
mail = Mail(app)

# Cache (Redis or Simple)
REDIS_URL = os.getenv("REDIS_URL")
if REDIS_URL:
    app.config["CACHE_TYPE"] = "RedisCache"
    app.config["CACHE_REDIS_URL"] = REDIS_URL
else:
    app.config["CACHE_TYPE"] = "SimpleCache"
cache = Cache(app)

# Uploads
app.config["UPLOAD_FOLDER"] = "uploads"
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# ================= INITIALIZE EXTENSIONS =================
db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# ================= DATABASE MODELS =================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(200))
    role = db.Column(db.String(20))

class Topic(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True)

class Performance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    topic_id = db.Column(db.Integer, db.ForeignKey("topic.id"))
    score = db.Column(db.Float)
    topic = db.relationship("Topic")

class Recommendation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer)
    topic_id = db.Column(db.Integer)
    suggestion = db.Column(db.Text)

class UploadedPDF(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer)
    filename = db.Column(db.String(200))
    content = db.Column(db.Text)

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer)
    role = db.Column(db.String(20))
    content = db.Column(db.Text)

class Flashcard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer)
    question = db.Column(db.Text)
    answer = db.Column(db.Text)

class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer)
    message = db.Column(db.Text)
    date = db.Column(db.DateTime)

# ================= LOGIN DECORATOR =================
def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("splash"))
            if role and session.get("role") != role:
                flash("Access denied", "danger")
                return redirect(url_for("splash"))
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ================= TOP NAV LINKS =================
def get_top_links(role):
    if role == "student":
        return [
            {"name": "Dashboard", "url": url_for("student_dashboard")},
            {"name": "Progress", "url": url_for("progress_page")},
            {"name": "Enter Scores", "url": url_for("enter_scores")},
            {"name": "Recommendations", "url": url_for("recommendations_page")},
            {"name": "Chatbot", "url": url_for("student_dashboard") + "#chatbot"},
            {"name": "Logout", "url": url_for("logout")}
        ]
    elif role == "lecturer":
        return [
            {"name": "Dashboard", "url": url_for("lecturer_dashboard")},
            {"name": "Analytics", "url": url_for("admin_analytics")},
            {"name": "Logout", "url": url_for("logout")}
        ]
    return []

# ================= AI HELPERS =================
def build_prompt(messages):
    return "\n".join([f"{m['role'].upper()}: {m['content']}" for m in messages])

@cache.memoize(timeout=300)
def cached_ai(prompt):
    if USE_AI:
        try:
            response = genai.text.generate(
                model="text-bison-001",
                prompt=prompt,
                max_output_tokens=500
            )
            return response.output_text
        except Exception as e:
            print("AI generation error:", e)
            return "AI service unavailable."
    else:
        return "AI service disabled."

def summarize_if_needed(history, sid):
    if len(history) < 20:
        return history
    prompt = "Summarize briefly:\n" + "\n".join([msg.content for msg in history])
    ChatMessage.query.filter_by(student_id=sid).delete()
    db.session.add(ChatMessage(student_id=sid, role="system",
                               content="Conversation summary: " + cached_ai(prompt)))
    db.session.commit()
    return ChatMessage.query.filter_by(student_id=sid).all()

def parse_pdf(path):
    text = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            if page.extract_text():
                text += page.extract_text() + "\n"
    return text


# ================= ROUTES =================
@app.route("/")
def splash():
    if "user_id" in session:
        return redirect(url_for("student_dashboard") if session["role"]=="student" else url_for("lecturer_dashboard"))
    return render_template("splash.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        email = request.form["email"]
        password = request.form["password"]
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            session["role"] = user.role
            session["name"] = user.name
            return redirect(url_for("student_dashboard") if user.role=="student" else url_for("lecturer_dashboard"))
        flash("Invalid credentials","danger")
    return render_template("login.html")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method=="POST":
        user = User(
            name=request.form["full_name"],
            email=request.form["email"],
            password=generate_password_hash(request.form["password"]),
            role=request.form["role"]
        )
        db.session.add(user)
        db.session.commit()
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("splash"))

@app.route("/forgot-password", methods=["GET","POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"]
        user = User.query.filter_by(email=email).first()
        if user:
            token = serializer.dumps(email, salt="reset-password")
            reset_link = url_for("reset_password", token=token, _external=True)
            msg = Message("IRRS Password Reset", sender=app.config['MAIL_USERNAME'], recipients=[email])
            msg.body = f"Hi {user.full_name},\n\nClick this link to reset your password:\n{reset_link}\n\nThis link expires in 1 hour."
            mail.send(msg)
        flash("If this email exists, a reset link has been sent!", "info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")

@app.route("/reset-password/<token>", methods=["GET","POST"])
def reset_password(token):
    try:
        email = serializer.loads(token, salt="reset-password", max_age=3600)  # 1 hour
    except:
        flash("Reset link invalid or expired", "danger")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form["password"]
        confirm = request.form["confirm_password"]
        if password != confirm:
            flash("Passwords do not match!", "danger")
            return redirect(url_for("reset_password", token=token))
        hashed = generate_password_hash(password)
        user = User.query.filter_by(email=email).first()
        user.password = hashed
        db.session.commit()
        flash("Password reset successful! You can now login.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)

# ================= STUDENT DASHBOARD =================
@app.route("/student_dashboard")
@login_required("student")
def student_dashboard():
    sid = session["user_id"]
    performances = Performance.query.filter_by(student_id=sid).all()
    avg = round(sum([p.score for p in performances])/len(performances),2) if performances else 0
    reminders = Reminder.query.filter(Reminder.date >= datetime.now()).all()
    return render_template("student_dashboard.html",
                           average=avg,
                           reminders=reminders,
                           top_links=get_top_links(session.get("role")),
                           user_name=session.get("name","Student"))
@app.route("/student_chat", methods=["POST"])
@login_required("student")
def student_chat():
    sid = session.get("user_id")

    data = request.get_json()
    msg = data.get("message", "").strip()

    if not msg:
        return jsonify({"reply": "Type a message"})

    # TEMP reply (you can improve later with AI)
    reply = "I received: " + msg

    return jsonify({"reply": reply})
# ================= PROGRESS =================
@app.route("/progress")
@login_required("student")
def progress_page():
    sid = session["user_id"]
    performances = Performance.query.filter_by(student_id=sid).all()
    data = {p.topic.name:p.score for p in performances}
    return render_template("progress.html",
                           data=data,
                           top_links=get_top_links(session.get("role")),
                           user_name=session.get("name","Student"))

# ================= ENTER SCORES =================
@app.route("/enter_scores")
@login_required("student")
def enter_scores():
    topics = Topic.query.all()
    return render_template("scores.html",
                           topics=topics,
                           top_links=get_top_links(session.get("role")),
                           user_name=session.get("name","Student"))

@app.route("/add_score_ajax",methods=["POST"])
@login_required("student")
def add_score_ajax():
    topic_name = request.form.get("topic")
    try:
        score_val = float(request.form.get("score"))
    except:
        return jsonify({"error":"Invalid score"}),400
    topic_obj = Topic.query.filter_by(name=topic_name).first()
    if not topic_obj:
        topic_obj = Topic(name=topic_name)
        db.session.add(topic_obj)
        db.session.commit()
    perf = Performance(student_id=session["user_id"], topic_id=topic_obj.id, score=score_val)
    db.session.add(perf)
    db.session.commit()
    return jsonify({"success":True})

@app.route("/delete_topic/<string:topic_name>",methods=["POST"])
@login_required("student")
def delete_topic(topic_name):
    sid = session["user_id"]
    perfs = Performance.query.filter_by(student_id=sid).join(Topic).filter(Topic.name==topic_name).all()
    for p in perfs:
        db.session.delete(p)
    db.session.commit()
    return jsonify({"success":True})

# ================= RECOMMENDATIONS =================
@app.route("/recommendations")
@login_required("student")
def recommendations_page():
    sid = session["user_id"]
    recs = Recommendation.query.filter_by(student_id=sid).all()
    return render_template("recommendations.html",
                           recommendations=recs,
                           top_links=get_top_links(session.get("role")),
                           user_name=session.get("name","Student"))

# ================= AI ANALYSIS =================
@app.route("/analyze_weakness")
@login_required("student")
def analyze_weakness():
    sid = session["user_id"]
    performances = Performance.query.filter_by(student_id=sid).all()
    data = {p.topic.name:p.score for p in performances}
    result = cached_ai(f"Analyze weaknesses from scores: {data}")
    return jsonify({"analysis": result})

# ================= CHATBOT HTTP =================
@app.route("/chatbot",methods=["POST"])
@login_required("student")
def chatbot():
    sid=session.get("user_id")
    data=request.get_json()
    msg=data.get("message","").strip()
    if not msg: return jsonify({"reply":"Type a message"})
    history=ChatMessage.query.filter_by(student_id=sid).all()
    if history: history=summarize_if_needed(history,sid)
    messages=[{"role":"system","content":"You are a university tutor"}]+[{"role":m.role,"content":m.content} for m in history]+[{"role":"user","content":msg}]
    reply=cached_ai(build_prompt(messages))
    db.session.add(ChatMessage(student_id=sid,role="user",content=msg))
    db.session.add(ChatMessage(student_id=sid,role="assistant",content=reply))
    db.session.commit()
    return jsonify({"reply":reply})

# ================= CHATBOT WEBSOCKET =================
@socketio.on("send_message")
def handle_message(data):
    sid=session.get("user_id")
    msg=data["message"]
    history=ChatMessage.query.filter_by(student_id=sid).all()
    history=summarize_if_needed(history,sid)
    messages=[{"role":"system","content":"You are a university tutor"}]+[{"role":m.role,"content":m.content} for m in history]+[{"role":"user","content":msg}]
    reply=cached_ai(build_prompt(messages))
    emit("receive_message",{"chunk":reply})
    db.session.add(ChatMessage(student_id=sid,role="user",content=msg))
    db.session.add(ChatMessage(student_id=sid,role="assistant",content=reply))
    db.session.commit()

# ================= PDF UPLOAD / SUMMARIZER =================
@app.route("/upload_pdf",methods=["POST"])
@login_required("student")
def upload_pdf():
    file=request.files["pdf"]
    path=os.path.join(app.config["UPLOAD_FOLDER"],file.filename)
    file.save(path)
    content=parse_pdf(path)
    db.session.add(UploadedPDF(student_id=session["user_id"],filename=file.filename,content=content))
    db.session.commit()
    return jsonify({"success":True})

@app.route("/summarize_pdf/<int:pdf_id>")
@login_required("student")
def summarize_pdf(pdf_id):
    pdf=UploadedPDF.query.get(pdf_id)
    summary=cached_ai(f"Summarize:\n{pdf.content[:4000]}")
    return jsonify({"summary":summary})

# ================= FLASHCARDS =================
@app.route("/generate_flashcards",methods=["POST"])
@login_required("student")
def generate_flashcards():
    topic=request.json.get("topic")
    result=cached_ai(f"Generate 5 flashcards about {topic}. Format Q: ... A: ...")
    return jsonify({"flashcards":result})

# ================= STUDY PLAN =================
@app.route("/generate_study_plan")
@login_required("student")
def generate_study_plan():
    sid=session["user_id"]
    performances=Performance.query.filter_by(student_id=sid).all()
    data={p.topic.name:p.score for p in performances}
    plan=cached_ai(f"Create a 2-week study plan based on: {data}")
    return jsonify({"plan":plan})

# ================= QUIZ =================
@app.route("/generate_quiz")
@login_required("student")
def generate_quiz():
    pdf_id=request.args.get("pdf_id")
    pdf=UploadedPDF.query.get(int(pdf_id))
    quiz=cached_ai(f"Generate 5 MCQs with answers from:\n{pdf.content[:4000]}")
    return jsonify({"quiz":quiz})

# ================= REMINDERS =================
@app.route("/generate_reminder")
@login_required("student")
def generate_reminder():
    sid=session["user_id"]
    reminder=Reminder(student_id=sid,message="Revise weak topics today",date=datetime.now()+timedelta(days=1))
    db.session.add(reminder)
    db.session.commit()
    return jsonify({"success":True})

# ================= AUTO GRADING =================
@app.route("/auto_grade",methods=["POST"])
@login_required("student")
def auto_grade():
    question=request.json.get("question")
    answer=request.json.get("answer")
    feedback=cached_ai(f"Grade this.\nQuestion:{question}\nAnswer:{answer}\nScore out of 10 and feedback.")
    return jsonify({"feedback":feedback})

# ================= ADMIN ANALYTICS =================
@app.route("/admin_analytics")
@login_required("lecturer")
def admin_analytics():
    return render_template("admin_analytics.html",
                           total_users=User.query.count(),
                           total_students=User.query.filter_by(role="student").count(),
                           total_messages=ChatMessage.query.count(),
                           top_links=get_top_links(session.get("role")),
                           user_name=session.get("name"))



# ================= LECTURER DASHBOARD =================
@app.route("/lecturer_dashboard")
@login_required("lecturer")
def lecturer_dashboard():
    students = User.query.filter_by(role="student").all()
    ranking = []
    total_scores = []
    topic_scores = {}
    weak_students = []

    for s in students:
        scores = Performance.query.filter_by(student_id=s.id).all()
        if scores:
            avg = sum([x.score for x in scores]) / len(scores)
            ranking.append({"student": s, "avg": round(avg, 2)})
            total_scores.append(avg)
        else:
            ranking.append({"student": s, "avg": 0})
            weak_students.append(s.name)

    ranking.sort(key=lambda x: x["avg"], reverse=True)
    avg_score = round(sum(total_scores)/len(total_scores), 2) if total_scores else 0
    weak_count = len(weak_students)

    units = list(topic_scores.keys())
    averages = [round(sum(scores)/len(scores), 2) for scores in topic_scores.values()]
    struggling_topics = [topic for topic, avg in zip(units, averages) if avg < 50]
    insight_message = f"Students are struggling in: {', '.join(struggling_topics)}" if struggling_topics else "Overall class performance is good."
    feedback = cached_ai(f"Lecturer overview: Class average {avg_score}, weak students {weak_count}, struggling topics: {struggling_topics}")

    return render_template(
        "lecturer_dashboard.html",
        rankings=ranking,
        avg_score=avg_score,
        weak_count=weak_count,
        total_students=len(students),
        units=units,
        averages=averages,
        top_links=get_top_links(session.get("role")),
        user_name=session.get("name", "Lecturer"),
        feedback=feedback or insight_message
    )

@app.route("/lecturer_recommendations/<int:student_id>", methods=["GET","POST"])
@login_required("lecturer")
def lecturer_recommendations(student_id):
    student = User.query.get(student_id)
    recs = Recommendation.query.filter_by(student_id=student_id).all()
    if request.method == "POST":
        suggestion = request.form.get("suggestion")
        if suggestion:
            new_rec = Recommendation(student_id=student_id, topic_id=0, suggestion=suggestion)
            db.session.add(new_rec)
            db.session.commit()
            flash("Recommendation added successfully!", "success")
            return redirect(url_for("lecturer_recommendations", student_id=student_id))
    return render_template("lecturer_recommendations.html", student=student, recommendations=recs)

# ================= RUN =================
if __name__=="__main__":
    with app.app_context(): db.create_all()
    app.debug=True
    socketio.run(app,host="0.0.0.0",port=5000)