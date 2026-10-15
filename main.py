import os
import sqlite3
import requests
import pytz
import dateutil.parser
import threading
import time
import json
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

# In-memory SQLite for blazing fast queries
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
    rates = {"USD": 1.0, "EUR": 1.03, "INR": 0.01179}
    
    page = 1
    total_pages = 1
    
    # 1. Fetch all pages and deduplicate by latest updated_at
    while page <= total_pages:
        params["page"] = page
        try:
            res = requests.get(base_url, params=params, timeout=15).json()
            total_pages = res.get("pages", 1)
            for order in res.get("orders", []):
                oid = order["id"]
                dt_updated = dateutil.parser.parse(order["updated_at"])
                
                if oid not in orders:
                    orders[oid] = (dt_updated, order)
                else:
                    if dt_updated > orders[oid][0]:
                        orders[oid] = (dt_updated, order)
        except Exception as e:
            print(f"Error fetching page {page}: {e}")
        page += 1

    orders = {oid: data for oid, (dt_updated, data) in orders.items()}

    # 2. Setup Database Schema
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
    
    ist = pytz.timezone("Asia/Kolkata")
    month_names = ["", "January", "February", "March", "April", "May", "June", 
                   "July", "August", "September", "October", "November", "December"]

    # 3. Load Data into DB
    for oid, o in orders.items():
        dt = dateutil.parser.parse(o["created_at"])
        dt_ist = dt.astimezone(ist)
        
        amount = o["amount"]
        rate = rates.get(o["currency"], 1.0)
        amount_usd = round(amount * rate, 6)
        
        quarter = f"Q{(dt_ist.month - 1) // 3 + 1}"
        
        cursor.execute(
            "INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                oid, o["customer"], o["region"], o["product"], o["qty"],
                amount_usd, o["currency"], o["status"],
                dt_ist.strftime("%Y-%m-%d %H:%M:%S"), dt_ist.year,
                dt_ist.month, month_names[dt_ist.month], quarter, dt_ist.strftime("%A")
            )
        )
    DB.commit()
    print(f"Successfully loaded {len(orders)} deduplicated orders into DB.")

# Load data in background
threading.Thread(target=fetch_and_load_data, daemon=True).start()

schema = """
Table orders (contains ALL orders, including paid, refunded, and void).
- id: unique order id
- customer: customer id
- region: one of North, South, East, West, Central
- product: exact product name as spelled in the database
- qty: quantity of items
- amount_usd: total amount in USD (amount * exchange rate)
- currency: original currency (USD, EUR, INR)
- status: 'paid', 'refunded', or 'void'
- created_at_ist: timestamp in Asia/Kolkata timezone
- year: year in IST
- month: month number (1-12) in IST
- month_name: full month name (e.g., 'March')
- quarter: 'Q1', 'Q2', 'Q3', 'Q4'
- day_of_week: day name (e.g., 'Monday')

Rules:
- "Revenue" means SUM(amount_usd) WHERE status = 'paid'.
- "Sold" items or "quantity sold" means SUM(qty) WHERE status = 'paid'.
- Dates and times must be evaluated in IST. Use year, month, month_name, or created_at_ist.
- Money is in USD.
- Return ONLY the raw SQL query. No markdown, no explanations.
"""

def get_answer(question: str) -> str:
    cursor = DB.cursor()
    for _ in range(60):
        try:
            cursor.execute("SELECT COUNT(*) FROM orders")
            if cursor.fetchone()[0] > 0:
                break
        except:
            pass
        time.sleep(0.5)

    prompt = f"""You are an expert SQL agent.
{schema}
The user will ask a question. Write a SQLite query to answer it.
Do NOT explain. ONLY output the SQL query.

Question: {question}
SQL:"""

    # Use standard HTTP requests instead of the openai library
    api_key = os.environ.get("AIPIPE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "Error: API key not found in environment variables."

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0
    }

    try:
        response = requests.post(
            "https://api.aipipe.org/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=15
        )
        response.raise_for_status()
        result = response.json()
        sql = result["choices"][0]["message"]["content"].strip()
        sql = sql.replace("```sql", "").replace("```", "").strip()
    except Exception as e:
        return f"Error calling LLM: {str(e)}"

    # Execute Query
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
        
        if not rows:
            return "0"
            
        cols = [description[0] for description in cursor.description]
        if len(rows) == 1 and len(cols) == 1:
            val = rows[0][0]
            if isinstance(val, float):
                return f"{val:.2f}"
            return str(val)
            
        result = []
        for row in rows:
            row_dict = {}
            for col, val in zip(cols, row):
                if isinstance(val, float):
                    val = round(val, 2)
                row_dict[col] = val
            result.append(str(row_dict))
            
        return "\n".join(result)
        
    except Exception as e:
        return f"Error executing query: {str(e)}"

class QuestionRequest(BaseModel):
    question: str

class AnswerResponse(BaseModel):
    answer: str

@app.post("/", response_model=AnswerResponse)
def ask_question(req: QuestionRequest):
    ans = get_answer(req.question)
    return {"answer": ans}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)