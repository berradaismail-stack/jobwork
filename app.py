import os
import json
import base64
import uuid
from datetime import datetime, date
from calendar import month_name

from flask import Flask, render_template, jsonify, request, make_response, send_from_directory
from dotenv import load_dotenv
import anthropic

load_dotenv()

app = Flask(__name__)

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
DATA_FILE = "/tmp/months.json"
UPLOADS_DIR = os.path.join(os.path.dirname(__file__), 'uploads')

os.makedirs(UPLOADS_DIR, exist_ok=True)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MA_UNIT_PRICE = 94.38   # MAD
TN_UNIT_PRICE = 27.6    # TND

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}

# ── Data helpers ───────────────────────────────────────────────────────────────

def load_months():
    if not os.path.exists(DATA_FILE):
        return [make_month_obj(today_month_id())]
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_months(months):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(months, f, ensure_ascii=False, indent=2)


def today_month_id():
    return date.today().strftime('%Y-%m')


def make_month_label(month_id):
    year, mon = month_id.split('-')
    return f"{month_name[int(mon)]} {year}"


def next_month_id(month_id):
    year, mon = int(month_id[:4]), int(month_id[5:])
    mon += 1
    if mon > 12:
        mon = 1
        year += 1
    return f"{year:04d}-{mon:02d}"


def make_month_obj(month_id):
    return {
        "id": month_id,
        "label": make_month_label(month_id),
        "status": "open",
        "approved_at": None,
        "screenshot_filename": None,
        "extracted": {"ma": {}, "tn": {}},
        "inputs": {
            "ma": {"forecast": None, "actual": None, "training_hours": None, "bonus_malus_pct": None,
                   "lbe_prod_hours": None, "lbe_training_hours": None, "lbe_bonus_malus_amount": None},
            "tn": {"forecast": None, "actual": None, "training_hours": None, "lcc_hours": None, "bonus_malus_pct": None,
                   "lbe_prod_hours": None, "lbe_training_hours": None, "lbe_lcc_hours": None, "lbe_bonus_malus_amount": None}
        },
        "calculated": {"ma": {}, "tn": {}},
        "delta": {"ma": None, "tn": None},
        "delta2": {"ma": None, "tn": None}
    }


def get_month(months, month_id):
    for m in months:
        if m['id'] == month_id:
            return m
    return None


# ── Business logic ─────────────────────────────────────────────────────────────

def calculate_market(inputs, unit_price, market):
    """Returns computed invoice fields, or None if any required input is missing."""
    forecast = inputs.get('forecast')
    actual   = inputs.get('actual')
    training = inputs.get('training_hours')
    bm_pct   = inputs.get('bonus_malus_pct')
    lcc      = inputs.get('lcc_hours', 0) if market == 'tn' else 0

    if any(v is None for v in [forecast, actual, training, bm_pct]):
        return None

    try:
        forecast = float(forecast)
        actual   = float(actual)
        training = float(training)
        bm_pct   = float(bm_pct)
        lcc      = float(lcc) if lcc is not None else 0.0

        invoiced_prod_hours = forecast if actual > forecast else actual  # min(actual, forecast)
        prod_cost           = invoiced_prod_hours * unit_price
        training_cost       = training * 0.80 * unit_price
        lcc_cost            = lcc * unit_price  # 0 for MA
        bonus_malus_amount  = (bm_pct / 100.0) * prod_cost
        total               = prod_cost + training_cost + lcc_cost + bonus_malus_amount

        return {
            "invoiced_prod_hours": round(invoiced_prod_hours, 4),
            "prod_cost":           round(prod_cost, 2),
            "training_cost":       round(training_cost, 2),
            "lcc_cost":            round(lcc_cost, 2),
            "bonus_malus_amount":  round(bonus_malus_amount, 2),
            "total":               round(total, 2),
        }
    except (TypeError, ValueError):
        return None


def calculate_lbe_delta(inputs, calc, unit_price, market):
    """Informational delta: my inputs total vs LBE total. Never blocks approval."""
    lbe_prod     = inputs.get('lbe_prod_hours')
    lbe_training = inputs.get('lbe_training_hours')
    lbe_lcc      = inputs.get('lbe_lcc_hours', 0) if market == 'tn' else 0
    lbe_bm_amount = inputs.get('lbe_bonus_malus_amount')

    if lbe_prod is None or lbe_training is None:
        return None
    if not calc:
        return None

    try:
        lbe_prod      = float(lbe_prod)
        lbe_training  = float(lbe_training)
        lbe_lcc       = float(lbe_lcc) if lbe_lcc is not None else 0.0
        lbe_bm_amount = float(lbe_bm_amount) if lbe_bm_amount is not None else 0.0

        lbe_total     = (lbe_prod * unit_price
                         + lbe_training * 0.80 * unit_price
                         + lbe_lcc * unit_price
                         + lbe_bm_amount)

        return round(calc['total'] - lbe_total, 2)
    except (TypeError, ValueError):
        return None


# ── Claude Vision extraction ───────────────────────────────────────────────────

EXTRACT_SYSTEM = """You are an invoice data extraction assistant for a BPO/contact-centre invoicing system. The user provides a screenshot of a monthly invoice containing figures for two markets: MA (Morocco) and TN (Tunisia).

Extract all numeric fields and return ONLY valid JSON with no markdown, no extra text, in this exact shape:
{
  "ma": {
    "invoiced_prod_hours": <number or null>,
    "prod_cost": <number or null>,
    "training_hours": <number or null>,
    "training_cost": <number or null>,
    "lcc_hours": 0,
    "lcc_cost": 0,
    "bonus_malus_pct": <number or null>,
    "bonus_malus_amount": <number or null>,
    "total": <number or null>
  },
  "tn": {
    "invoiced_prod_hours": <number or null>,
    "prod_cost": <number or null>,
    "training_hours": <number or null>,
    "training_cost": <number or null>,
    "lcc_hours": <number or null>,
    "lcc_cost": <number or null>,
    "bonus_malus_pct": <number or null>,
    "bonus_malus_amount": <number or null>,
    "total": <number or null>
  }
}

Rules:
- bonus_malus_pct: positive number for bonus, negative for malus (e.g. 2.5 or -1.5)
- All monetary values: plain numbers without currency symbols
- Use null for any field you cannot confidently read from the screenshot
- lcc_hours and lcc_cost are 0 for MA (Morocco does not have the LCC role)"""


def extract_invoice(image_path):
    ext = os.path.splitext(image_path)[1].lower()
    media_map = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                 '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp'}
    media_type = media_map.get(ext, 'image/png')

    with open(image_path, 'rb') as f:
        image_data = base64.standard_b64encode(f.read()).decode('utf-8')

    message = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=1024,
        system=EXTRACT_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                {"type": "text", "text": "Extract all invoice figures for MA and TN markets from this screenshot."}
            ]
        }]
    )

    raw = message.content[0].text.strip()
    raw = raw.split('\n', 1)[-1] if raw.startswith('```') else raw
    raw = raw.rsplit('```', 1)[0] if raw.endswith('```') else raw
    return json.loads(raw.strip())


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    response = make_response(render_template('index.html'))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@app.route('/api/months')
def api_months():
    return jsonify(load_months())


@app.route('/api/extract', methods=['POST'])
def api_extract():
    if 'screenshot' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file     = request.files['screenshot']
    month_id = request.form.get('month_id')
    if not month_id:
        return jsonify({'error': 'month_id required'}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({'error': 'File must be an image (PNG, JPG, WEBP)'}), 400

    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == 'your_anthropic_api_key_here':
        return jsonify({'error': 'ANTHROPIC_API_KEY not configured in .env'}), 500

    months = load_months()
    month  = get_month(months, month_id)
    if not month:
        return jsonify({'error': 'Month not found'}), 404
    if month['status'] == 'approved':
        return jsonify({'error': 'Month is already approved'}), 400

    filename  = f"{month_id}_{uuid.uuid4().hex[:8]}{ext}"
    save_path = os.path.join(UPLOADS_DIR, filename)
    file.save(save_path)

    try:
        extracted = extract_invoice(save_path)
    except Exception as e:
        return jsonify({'error': f'Extraction failed: {e}'}), 500

    month['screenshot_filename'] = filename
    month['extracted']           = extracted
    save_months(months)

    return jsonify({'extracted': extracted, 'filename': filename})


@app.route('/api/save/<month_id>', methods=['POST'])
def api_save(month_id):
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    months = load_months()
    month  = get_month(months, month_id)
    if not month:
        return jsonify({'error': 'Month not found'}), 404
    if month['status'] == 'approved':
        return jsonify({'error': 'Month is already approved'}), 400

    month['inputs'] = data.get('inputs', month['inputs'])

    calc_ma = calculate_market(month['inputs']['ma'], MA_UNIT_PRICE, 'ma')
    calc_tn = calculate_market(month['inputs']['tn'], TN_UNIT_PRICE, 'tn')

    month['calculated']['ma'] = calc_ma or {}
    month['calculated']['tn'] = calc_tn or {}

    def delta(extracted, calculated):
        ext_t  = (extracted or {}).get('total')
        calc_t = (calculated or {}).get('total')
        if ext_t is None or calc_t is None:
            return None
        return round(ext_t - calc_t, 2)

    month['delta']['ma'] = delta(month['extracted'].get('ma'), calc_ma)
    month['delta']['tn'] = delta(month['extracted'].get('tn'), calc_tn)

    if 'delta2' not in month:
        month['delta2'] = {'ma': None, 'tn': None}
    month['delta2']['ma'] = calculate_lbe_delta(month['inputs']['ma'], calc_ma, MA_UNIT_PRICE, 'ma')
    month['delta2']['tn'] = calculate_lbe_delta(month['inputs']['tn'], calc_tn, TN_UNIT_PRICE, 'tn')

    save_months(months)
    return jsonify({'calculated': month['calculated'], 'delta': month['delta'], 'delta2': month['delta2']})


@app.route('/api/approve/<month_id>', methods=['POST'])
def api_approve(month_id):
    months = load_months()
    month  = get_month(months, month_id)
    if not month:
        return jsonify({'error': 'Month not found'}), 404
    if month['status'] == 'approved':
        return jsonify({'error': 'Already approved'}), 400
    if month['delta']['ma'] != 0 or month['delta']['tn'] != 0:
        return jsonify({'error': 'Cannot approve: deltas are not zero'}), 400

    month['status']      = 'approved'
    month['approved_at'] = datetime.utcnow().isoformat() + 'Z'

    next_id = next_month_id(month_id)
    if not get_month(months, next_id):
        months.append(make_month_obj(next_id))

    save_months(months)
    return jsonify({'status': 'approved', 'next_month_id': next_id})


@app.route('/api/screenshot/<filename>')
def api_screenshot(filename):
    if '..' in filename or '/' in filename:
        return jsonify({'error': 'Invalid filename'}), 400
    return send_from_directory(UPLOADS_DIR, filename)


if __name__ == '__main__':
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == 'your_anthropic_api_key_here':
        print("\n  WARNING: No Anthropic API key found. Edit the .env file.\n")
    months = load_months()
    save_months(months)
    port = int(os.getenv('PORT', 8083))
    print("Starting Invoice Verifier...")
    print(f"Open your browser at: http://localhost:{port}\n")
    app.run(debug=False, host='0.0.0.0', port=port)
