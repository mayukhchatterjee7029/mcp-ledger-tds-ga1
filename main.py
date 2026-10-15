import sqlite3
import requests
import pytz
import dateutil.parser
import threading
import time
import re
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
DB = sqlite3.connect(":memory:", check_same_thread=False)

def fetch_and_load_data():
    base_url = "https://exam.sanand.workers.dev/questionData"
    params = {
        "email": "24f3003132@ds.study.iitm.ac.in",
        "quizSign": "ckOc7kBsruLGryWtCv2nkuBFHbT5XqsGZAUm5HuLW5dEQWxAoAXkkI9gTkK2q8julkieVaySMrHLPE7fmtUPH9uOGRxG1/owRf0VxsNz9w31QUVo6rlsUa60u8OCWNToumMuF/tCuCdIg9aO4rrRqHRvFYi3w5m4raF28abwuu9/zw8kYK3QZIc9RTEiX12bfpfHq0jDmx0s8s6elEf325St2jIIPLz37KIkltjmEjTT+/2k5nh97gFpc2o9jnfX/xVo2nKkdvW/gArySsUwQxwzc6RmIlCVfSx5fVm22dDTx7Rwk069FdeW8OGx7GAMAed/mo+NyfqWo7cbYBvUPQ==",
        "questionId": "q-ledger-agent-server",
        "path": "/orders"
    }
    
    orders = {}
    page = 1
    total_pages = 1
    
    while page <= total_pages:
        params["page"] = page
        try:
            res = requests.get(base_url, params=params, timeout=10).json()
            total_pages = res["pages"]
            for order in res["orders"]:
                oid = order["id"]
                dt_updated = dateutil.parser.parse(order["updated_at"])
                if oid not in orders or dt_updated > orders[oid][0]:
                    orders[oid] = (dt_updated, order)
        except Exception as e:
            print(f"Error fetching page {page}: {e}")
        page += 1

    orders = {oid: data for oid, (dt_updated, data) in orders.items()}

    cursor = DB.cursor()
    cursor.execute("""
        CREATE TABLE orders (
            id TEXT PRIMARY KEY,
            customer TEXT,
            region TEXT,
            product TEXT,
            qty INTEGER,
            amount_usd REAL,
            currency TEXT,
            status TEXT,
            created_at_ist TEXT,
            year INTEGER,
            month INTEGER,
            month_name TEXT,
            quarter TEXT,
            day_of_week TEXT
        )
    """)
    
    rates = {"USD": 1.0, "EUR": 1.03, "INR": 0.01179}
    ist = pytz.timezone("Asia/Kolkata")
    month_names = ["", "January", "February", "March", "April", "May", "June", 
                   "July", "August", "September", "October", "November", "December"]

    for oid, o in orders.items():
        dt = dateutil.parser.parse(o["created_at"])
        dt_ist = dt.astimezone(ist)
        amount_usd = o["amount"] * rates.get(o["currency"], 1.0)
        quarter = f"Q{(dt_ist.month - 1) // 3 + 1}"
        
        cursor.execute(
            "INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, o["customer"], o["region"], o["product"], o["qty"],
             amount_usd, o["currency"], o["status"],
             dt_ist.strftime("%Y-%m-%d %H:%M:%S"), dt_ist.year,
             dt_ist.month, month_names[dt_ist.month], quarter, dt_ist.strftime("%A"))
        )
    DB.commit()
    print(f"Loaded {len(orders)} orders into DB.")

threading.Thread(target=fetch_and_load_data, daemon=True).start()

def parse_question_to_sql(question: str) -> str:
    q = question.lower()
    
    # 1. Determine Metric
    if any(word in q for word in ["revenue", "earnings", "money", "total amount", "refunds", "refunded amount"]):
        metric = "SUM(amount_usd)"
    elif any(word in q for word in ["sold", "quantity", "how many", "units", "total qty"]):
        metric = "SUM(qty)"
    elif "number of orders" in q or "how many orders" in q:
        metric = "COUNT(id)"
    else:
        metric = "SUM(amount_usd)" # Default fallback
        
    # 2. Determine Status
    if "refunded" in q or "refunds" in q:
        status = "'refunded'"
    elif "void" in q:
        status = "'void'"
    else:
        status = "'paid'" # Revenue/sold implies paid
        
    # 3. Determine Filters
    conditions = [f"status = {status}"]
    
    regions = ["north", "south", "east", "west", "central"]
    for r in regions:
        if r in q:
            conditions.append(f"region = '{r.capitalize()}'")
            
    products = ["juicer", "grinder", "toaster", "kettle", "rice cooker", "blender", "mixer", "air fryer"]
    for p in products:
        if p in q:
            conditions.append(f"product = '{p.title()}'")
            
    # Time parsing
    month_match = re.search(r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\b', q)
    if month_match:
        conditions.append(f"month_name = '{month_match.group(1).capitalize()}'")
        
    year_match = re.search(r'\b(2025|2026|2027)\b', q)
    if year_match:
        conditions.append(f"year = {year_match.group(1)}")
        
    return f"SELECT {metric} FROM orders WHERE {' AND '.join(conditions)}"

def get_answer(question: str) -> str:
    cursor = DB.cursor()
    # Wait for DB to load
    for _ in range(30):
        try:
            cursor.execute("SELECT COUNT(*) FROM orders")
            if cursor.fetchone()[0] > 0:
                break
        except:
            pass
        time.sleep(0.5)

    sql = parse_question_to_sql(question)
    
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
        if not rows or rows[0][0] is None:
            return "0"
            
        val = rows[0][0]
        if isinstance(val, float):
            return f"{val:.2f}" # Money correct to the cent
        return str(val)
    except Exception as e:
        return f"Error: {e}"

class QuestionRequest(BaseModel):
    question: str

class AnswerResponse(BaseModel):
    answer: str

@app.post("/", response_model=AnswerResponse)
def ask_question(req: QuestionRequest):
    return {"answer": get_answer(req.question)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)