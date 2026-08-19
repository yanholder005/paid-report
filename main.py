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

def send_admin_alert(msg, email):
    try:
        resend.api_key = os.environ.get("RESEND_API_KEY")
        resend.Emails.send({
            "from": "Yan Holder <yan@yanholder.com>",
            "to": ["yan@yanholder.com"],
            "subject": "⚠️ ALARM: Paid Report Failed",
            "html": f"<p>Error: {msg}</p><p>Email: {email}</p>"
        })
    except:
        pass

async def get_coordinates(city, nation):
    loc_query = f"{city}, {nation}"
    for _ in range(3):
        try:
            location = await asyncio.to_thread(geolocator.geocode, loc_query)
            if location: return location
        except:
            await asyncio.sleep(1) 
    raise Exception(f"Could not locate '{loc_query}'.")

async def get_chart_data(name, year, month, day, hour, minute, city, nation):
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

    return "\n".join(lines)

async def process_paid_report(data: PaidReportRequest):
    # THE 15-MINUTE PREMIUM ANTICIPATION DELAY
    await asyncio.sleep(2)

    try:
        year, month, day = map(int, data.date.split("-"))
        hour, minute = map(int, data.time.split(":"))
        formatted_dob = datetime.date(year, month, day).strftime("%B %d, %Y")

        chart_data = await get_chart_data(data.name, year, month, day, hour, minute, data.city, data.nation)

        client = await asyncio.to_thread(get_gspread_client)
        paid_sheet = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("PaidReports")
        settings = client.open_by_key(os.environ.get("GOOGLE_SHEET_ID")).worksheet("Settings")
        
        # --- THE DYNAMIC PROMPT TOGGLE ---
        # Grabs B2 if they bought the bump, otherwise grabs B1
        if data.bump:
            master_prompt = settings.acell('B2').value
        else:
            master_prompt = settings.acell('B1').value

        context_string = f"Deep Dive Context from User: {data.question}\n"

        # --- THE CROSS-REFERENCE BACKWARD LOOKUP ---
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
        except Exception as e:
            print(f"Warning: Could not fetch past records for cross-reference: {e}")
            pass

        user_prompt = f"Name: {data.name}\nDOB: {formatted_dob}\nTime: {data.time}\nLocation: {data.city}\n\n{context_string}\n\nCHART DATA:\n{chart_data}"

        genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
        model = genai.GenerativeModel("gemini-3.1-pro-preview")
        response = await model.generate_content_async(f"{master_prompt}\n\n{user_prompt}")
        report_markdown = response.text

        # Log to PaidReports Google Sheet
        row = [data.name, data.email, data.date, data.time, f"{data.city}, {data.nation}", data.question, "Yes" if data.bump else "No", datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")]
        await asyncio.to_thread(paid_sheet.append_row, row)

        # Build PDF
        html_content = markdown.markdown(report_markdown)
        pdf_html = f"""
        <html>
        <head>
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=Figtree:wght@400;600&display=swap');
                
                /* 1. Apply beige background to ALL pages, including the margins */
                @page {{ 
                    size: A4; 
                    margin: 2.5cm 2.2cm; 
                    background-color: #FDFBF7; 
                }}
                
                /* 2. Create a special named page for the cover with zero margins */
                @page cover {{ 
                    margin: 0; 
                    background-color: #FDFBF7;
                }}
                
                body {{ 
                    font-family: 'Figtree', sans-serif; 
                    color: #111; 
                    font-size: 14.5px; 
                    line-height: 1.8; 
                }}
                
                /* 3. Assign the cover container to the named cover page */
                .cover-container {{ 
                    page: cover; 
                    height: 100vh; 
                    width: 100vw; 
                    display: block; 
                    break-after: page; 
                    overflow: hidden;
                }}
                
                /* 4. The bulletproof WeasyPrint image fix: an object-fit img tag */
                .cover-container img {{
                    width: 100%;
                    height: 100%;
                    object-fit: cover;
                    display: block;
                }}
                
                h1, h2, h3 {{ 
                    font-family: 'Bricolage Grotesque', sans-serif; 
                    color: #000; 
                    margin-top: 35px; 
                    margin-bottom: 15px; 
                }}
                
                h2 {{ font-size: 22px; border-bottom: 1px solid #ddd; padding-bottom: 5px; }}
                h3 {{ font-size: 18px; }}
                p {{ margin-bottom: 16px; text-align: justify; }}
                strong {{ font-weight: 700; color: #000; }}
                
                .header-box {{ 
                    text-align: left; 
                    margin-bottom: 40px; 
                    padding-bottom: 20px; 
                    border-bottom: 2px dashed #ccc; 
                }}
                
                .header-title {{ font-family: 'Bricolage Grotesque', sans-serif; font-size: 32px; font-weight: 700; line-height: 1.2; margin-bottom: 8px; }}
                .header-sub {{ font-size: 13px; font-weight: 600; color: #555; text-transform: uppercase; letter-spacing: 1.5px; }}
            </style>
        </head>
        <body>
            <!-- The Bulletproof Cover Image Container -->
            <div class="cover-container">
                <img src="https://yanholder.com/assets/images/image03.jpg?v=84d19ef7" alt="Astrological Blueprint Cover" />
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
        
        pdf_file = HTML(string=pdf_html).write_pdf()

        # Email PDF via Resend
        resend.api_key = os.environ.get("RESEND_API_KEY")
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

    except Exception as e:
        print(f"Error: {e}")
        send_admin_alert(str(e), data.email)

@app.post("/generate-paid")
async def generate_paid(data: PaidReportRequest, bg_tasks: BackgroundTasks):
    bg_tasks.add_task(process_paid_report, data)
    return {"success": True}

@app.get("/")
async def health_check(): return {"status": "paid engine active"}
