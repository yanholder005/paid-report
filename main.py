from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from kerykeion import AstrologicalSubject
from geopy.geocoders import ArcGIS
from timezonefinder import TimezoneFinder
import google.generativeai as genai
import resend
import gspread
from google.oauth2.service_account import Credentials
import asyncio
import os
import json
import time
import random
import datetime
import markdown
from weasyprint import HTML

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

geolocator = ArcGIS(timeout=5)
tf = TimezoneFinder()
GSPREAD_CLIENT = None

class PaidReportRequest(BaseModel):
    name: str
    email: str
    date: str
    time: str
    city: str
    nation: str
    question: str
    bump: bool

def get_gspread_client():
    global GSPREAD_CLIENT
    if GSPREAD_CLIENT is None:
        creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        GSPREAD_CLIENT = gspread.authorize(creds)
    return GSPREAD_CLIENT

def exponential_backoff_retry(func, *args, max_retries=4, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep((2 ** attempt) + random.uniform(0, 1))

def send_admin_alert(msg, email, status_msg=""):
    try:
        resend.api_key = os.environ.get("RESEND_API_KEY")
        resend.Emails.send({
            "from": "Yan Holder <yan@yanholder.com>",
            "to": ["yan@yanholder.com"],
            "subject": "⚠️ ALARM: Paid Report Failed",
            "html": f"<p>Error: {msg}</p><p>Email: {email}</p><p>Status: {status_msg}</p>"
        })
    except:
        pass

# --- NEW: SHEET STATE MANAGERS ---
def update_request_status(email, new_status):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("PaidReports")
        emails = sheet.col_values(2) # Column 2 is Email
        
        # Search backwards to find the MOST RECENT purchase if they bought twice
        row_index = len(emails) - emails[::-1].index(email) 
        
        def update(): sheet.update_cell(row_index, 9, new_status) # Column 9 is Status
        exponential_backoff_retry(update)
    except Exception as e:
        print(f"Failed to update status for {email}: {e}")

async def get_coordinates(city, nation):
    loc_query = f"{city}, {nation}"
    for _ in range(3):
        try:
            location = await asyncio.to_thread(geolocator.geocode, loc_query)
            if location: return location
        except:
            await asyncio.sleep(1) 
    raise Exception(f"Could not locate '{loc_query}'.")

async def get_chart_data(name, year, month, day, hour, minute, city, nation, bump=False):
    location = await get_coordinates(city, nation)
    tz_str = await asyncio.to_thread(tf.timezone_at, lng=location.longitude, lat=location.latitude)
    
    subject = await asyncio.to_thread(AstrologicalSubject, name, year, month, day, hour, minute, lng=location.longitude, lat=location.latitude, tz_str=tz_str, city=city)
    
    now_utc = datetime.datetime.utcnow()
    subject_transit = await asyncio.to_thread(AstrologicalSubject, "Transit", now_utc.year, now_utc.month, now_utc.day, now_utc.hour, now_utc.minute, lng=0.0, lat=51.5, tz_str="UTC", city="London")

    def deg_to_d_m(deg):
        d = int(deg)
        m = int(round((deg - d) * 60))
        if m == 60: d += 1; m = 0
        return f"{d}°{m:02d}’"

    def get_abs_pos(subj, attr):
        obj = getattr(subj, attr, None)
        return getattr(obj, "abs_pos", 0) if not isinstance(obj, dict) else obj.get("abs_pos", 0)

    lines = []
    points = [("Sun", "sun"), ("Moon", "moon"), ("Mercury", "mercury"), ("Venus", "venus"), ("Mars", "mars"), ("Jupiter", "jupiter"), ("Saturn", "saturn"), ("Uranus", "uranus"), ("Neptune", "neptune"), ("Pluto", "pluto"), ("North Node", "true_node"), ("Chiron", "chiron")]
    
    for d_name, a_name in points:
        obj = getattr(subject, a_name, None)
        if obj:
            sign = getattr(obj, "sign", "")
            pos = getattr(obj, "position", 0)
            house = getattr(obj, "house", "")
            h_str = f", in {house} House" if house else ""
            lines.append(f"{d_name} in {sign} {deg_to_d_m(pos)}{h_str}")

    transit_entities = [{"name": n, "abs_pos": get_abs_pos(subject_transit, a)} for n, a in points if getattr(subject_transit, a, None)]
    natal_entities = [{"name": n, "abs_pos": get_abs_pos(subject, a)} for n, a in points if getattr(subject, a, None)]

    if transit_entities:
        lines.append("\n=== CURRENT TRANSITS TO NATAL ===")
        for t_ent in transit_entities:
            for n_ent in natal_entities:
                diff = abs(t_ent["abs_pos"] - n_ent["abs_pos"])
                diff = min(diff, 360 - diff)
                max_orb = 3 if t_ent["name"] in ["Sun", "Moon"] else 2
                for asp_name, asp_angle in [("Conjunction", 0), ("Square", 90), ("Opposition", 180)]:
                    if abs(diff - asp_angle) <= max_orb:
                        lines.append(f"Transit {t_ent['name']} {asp_name} Natal {n_ent['name']}")

    if bump:
        lines.append("\n=== 6-MONTH TRANSIT FORECAST DATA ===")
        for i in range(1, 7):
            m_math = now_utc.month - 1 + i
            target_year = now_utc.year + (m_math // 12)
            target_month = (m_math % 12) + 1
            target_date = datetime.datetime(target_year, target_month, 1, 12, 0)
            month_name = target_date.strftime('%B %Y')
            
            lines.append(f"\n--- {month_name} ---")
            
            future_subj = await asyncio.to_thread(AstrologicalSubject, f"T_{i}", target_year, target_month, 1, 12, 0, lng=0.0, lat=51.5, tz_str="UTC", city="London")
            
            slow_points = [("Mars", "mars"), ("Jupiter", "jupiter"), ("Saturn", "saturn"), ("Uranus", "uranus"), ("Neptune", "neptune"), ("Pluto", "pluto"), ("North Node", "true_node")]
            future_ents = [{"name": n, "abs_pos": get_abs_pos(future_subj, a)} for n, a in slow_points if getattr(future_subj, a, None)]
            
            month_has_transits = False
            for f_ent in future_ents:
                for n_ent in natal_entities:
                    diff = abs(f_ent["abs_pos"] - n_ent["abs_pos"])
                    diff = min(diff, 360 - diff)
                    max_orb = 2 
                    for asp_name, asp_angle in [("Conjunction", 0), ("Square", 90), ("Opposition", 180)]:
                        if abs(diff - asp_angle) <= max_orb:
                            lines.append(f"Transit {f_ent['name']} {asp_name} Natal {n_ent['name']}")
                            month_has_transits = True
            
            if not month_has_transits:
                lines.append("No exact hard aspects forming this month. (Focus on ongoing macro transits).")

    return "\n".join(lines)

async def process_paid_report(data: PaidReportRequest, skip_delay=False):
    # If recovering a failed task, we skip the 15 minute wait
    if not skip_delay:
        await asyncio.sleep(900)

    try:
        await asyncio.to_thread(update_request_status, data.email, "PROCESSING")

        year, month, day = map(int, data.date.split("-"))
        hour, minute = map(int, data.time.split(":"))
        formatted_dob = datetime.date(year, month, day).strftime("%B %d, %Y")

        now_date = datetime.datetime.utcnow()
        age = now_date.year - year - ((now_date.month, now_date.day) < (month, day))
        
        prof_num = (age % 12) + 1
        suffixes = {1: 'st', 2: 'nd', 3: 'rd'}
        suffix = suffixes.get(prof_num if prof_num < 20 else prof_num % 10, 'th')
        profection_house = f"{prof_num}{suffix} House"

        chart_data = await get_chart_data(data.name, year, month, day, hour, minute, data.city, data.nation, data.bump)

        client = await asyncio.to_thread(get_gspread_client)
        settings = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("Settings")
        
        master_prompt = settings.acell('B2').value if data.bump else settings.acell('B1').value
        context_string = f"Deep Dive Context from User: {data.question}\n"

        try:
            free_sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("Sheet1")
            all_values = free_sheet.get_all_values()
            
            past_record = None
            for row in reversed(all_values):
                if len(row) > 5 and row[5].strip().lower() == data.email.strip().lower():
                    past_record = row
                    break
            
            if past_record:
                orig_q = past_record[4] if len(past_record) > 4 else ''
                orig_cats = past_record[6] if len(past_record) > 6 else ''
                orig_report = past_record[8] if len(past_record) > 8 else ''
                
                if orig_report:
                    context_string += f"\n--- MEMORY: PREVIOUS FREE REPORT CONTEXT ---\n"
                    context_string += "You previously gave this user a short free reading. You MUST ensure this new paid deep-dive expands upon, validates, and aligns with what you already told them in the free report below.\n\n"
                    context_string += f"Original Focus Area Selected: {orig_cats}\n"
                    context_string += f"Original Situation: {orig_q}\n\n"
                    context_string += f"The Free Report They Already Read:\n{orig_report}\n"
                    context_string += f"--- END MEMORY ---\n\n"
        except:
            pass

        user_prompt = f"Name: {data.name}\nDOB: {formatted_dob}\nTime: {data.time}\nLocation: {data.city}\nCurrent Age: {age}\nCurrent Profection Year: {profection_house}\n\n{context_string}\n\nCHART DATA:\n{chart_data}"

        genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
        model = genai.GenerativeModel("gemini-3.1-pro-preview")
        
        # MICRO-RETRY LOOP: API Generation
        report_markdown = ""
        for attempt in range(3):
            try:
                response = await model.generate_content_async(f"{master_prompt}\n\n{user_prompt}")
                report_markdown = response.text
                break
            except Exception as e:
                if attempt == 2: raise Exception(f"Gemini API Error: {e}")
                await asyncio.sleep(5)

        # Build PDF
        html_content = markdown.markdown(report_markdown)
        pdf_html = f"""
        <html>
        <head>
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=Figtree:wght@400;600&display=swap');
                
                html, body {{ margin: 0; padding: 0; background-color: #FDFBF7; }}
                @page {{ size: A4; margin: 2.5cm 2.2cm; background-color: #FDFBF7; @bottom-center {{ content: counter(page); font-family: 'Figtree', sans-serif; font-size: 13px; color: rgba(17, 17, 17, 0.5); }} }}
                @page cover {{ margin: 0; background-color: #FDFBF7; @bottom-center {{ content: none; }} }}
                
                body {{ font-family: 'Figtree', sans-serif; color: #111; font-size: 14.5px; line-height: 1.8; }}
                .cover-container {{ page: cover; width: 100vw; height: 100vh; display: block; page-break-after: always; overflow: hidden; }}
                .cover-container img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
                .content-page {{ counter-reset: page 1; }}
                
                h1, h2, h3 {{ font-family: 'Bricolage Grotesque', sans-serif; color: #000; margin-top: 35px; margin-bottom: 15px; }}
                h2 {{ font-size: 22px; border-bottom: 1px solid #ddd; padding-bottom: 5px; }}
                h3 {{ font-size: 18px; }}
                p {{ margin-bottom: 16px; text-align: justify; }}
                strong {{ font-weight: 700; color: #000; }}
                
                .header-box {{ text-align: left; margin-bottom: 40px; padding-bottom: 20px; border-bottom: 2px dashed #ccc; }}
                .header-title {{ font-family: 'Bricolage Grotesque', sans-serif; font-size: 32px; font-weight: 700; line-height: 1.2; margin-bottom: 8px; }}
                .header-sub {{ font-size: 13px; font-weight: 600; color: #555; text-transform: uppercase; letter-spacing: 1.5px; }}
            </style>
        </head>
        <body>
            <div class="cover-container">
                <img src="https://yanholder.com/assets/images/image12.jpg?v=104f716e" alt="Astrological Blueprint Cover" />
            </div>
            
            <div class="content-page">
                <div class="header-box">
                    <div class="header-title">{data.name}'s Astrological<br>Blueprint</div>
                    <div class="header-sub">BORN: {formatted_dob} {data.time} • {data.city}</div>
                </div>
                {html_content}
            </div>
        </body>
        </html>
        """
        
        # MICRO-RETRY LOOP: PDF Build & Email Send
        resend.api_key = os.environ.get("RESEND_API_KEY")
        for attempt in range(3):
            try:
                pdf_file = HTML(string=pdf_html).write_pdf()
                email_body = f"""
                <p>Hi {data.name},</p>
                <p>Your complete astrological blueprint has been compiled, formatted, and secured.</p>
                <p>Attached to this email is your final PDF report. It contains the exact architecture of your chart, the specific blocks currently running in your subconscious, and the concrete direction required to clear them.</p>
                <p>Take your time with this. It is a lot of information.</p>
                <p>Best,<br>Yan</p>
                """
                resend.Emails.send({
                    "from": "Yan Holder <yan@yanholder.com>",
                    "to": [data.email],
                    "subject": f"{data.name}, Your Complete Astrological Blueprint is Ready",
                    "html": email_body,
                    "attachments": [{"filename": f"{data.name}_Blueprint.pdf", "content": list(pdf_file)}]
                })
                break
            except Exception as e:
                if attempt == 2: raise Exception(f"PDF/Email Error: {e}")
                await asyncio.sleep(5)

        # Mark as delivered!
        await asyncio.to_thread(update_request_status, data.email, "DELIVERED")

    except Exception as e:
        print(f"Process Error: {e}")
        await asyncio.to_thread(update_request_status, data.email, f"FAILED: {str(e)[:40]}")
        send_admin_alert(str(e), data.email, "Marked as FAILED in sheet.")

@app.post("/generate-paid")
async def generate_paid(data: PaidReportRequest, bg_tasks: BackgroundTasks):
    # Log to Google Sheets IMMEDIATELY with 'QUEUED' status before anything else happens
    try:
        client = get_gspread_client()
        paid_sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("PaidReports")
        row = [data.name, data.email, data.date, data.time, f"{data.city}, {data.nation}", data.question, "Yes" if data.bump else "No", datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), "QUEUED"]
        
        def append(): paid_sheet.append_row(row)
        exponential_backoff_retry(append)
    except Exception as e:
        print(f"Failed to log initial request: {e}")
        # Even if logging fails, we proceed so the user gets their product
    
    bg_tasks.add_task(process_paid_report, data)
    return {"success": True}

@app.get("/process-queue")
async def process_queue(bg_tasks: BackgroundTasks):
    """
    Hit this endpoint via cronjob to auto-recover any crashed or failed reports.
    It scans the sheet, and if it finds a FAILED task, or a QUEUED task older than 20 mins, it retries it.
    """
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("PaidReports")
        records = sheet.get_all_values()
        
        now_utc = datetime.datetime.utcnow()
        recovered_count = 0

        for i, row in enumerate(records):
            if i == 0 or len(row) < 9: 
                continue 
            
            email = row[1]
            timestamp_str = row[7]
            status = row[8]
            
            try:
                ts = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                minutes_elapsed = (now_utc - ts).total_seconds() / 60
            except:
                continue
            
            if "FAILED" in status or (status == "QUEUED" and minutes_elapsed > 20):
                city_nation = row[4].split(", ")
                city = city_nation[0]
                nation = city_nation[1] if len(city_nation) > 1 else ""
                
                data = PaidReportRequest(
                    name=row[0], email=email, date=row[2], time=row[3], 
                    city=city, nation=nation, question=row[5], bump=(row[6] == "Yes")
                )
                
                sheet.update_cell(i + 1, 9, "RETRYING")
                bg_tasks.add_task(process_paid_report, data, skip_delay=True)
                recovered_count += 1
                
        return {"status": f"Triggered recovery for {recovered_count} records."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/")
async def health_check(): return {"status": "paid engine active"}
