from flask import Flask, jsonify
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
import time

app = Flask(__name__)
CORS(app)

def nse_session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.nseindia.com',
    })
    s.get('https://www.nseindia.com', timeout=10)
    time.sleep(1)
    return s

@app.route('/price/<symbol>')
def price(symbol):
    try:
        s = nse_session()
        # NSE chart data endpoint
        end = int(datetime.now().timestamp())
        start = int((datetime.now() - timedelta(days=365*20)).timestamp())
        url = f'https://charting.nseindia.com/Charts/GetEODChart?identifier={symbol}_EQ&start={start}&end={end}'
        r = s.get(url, timeout=20)
        return jsonify({'status': r.status_code, 'raw': r.text[:1000]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/quote/<symbol>')
def quote(symbol):
    try:
        s = nse_session()
        url = f'https://www.nseindia.com/api/quote-equity?symbol={symbol}'
        r = s.get(url, timeout=15)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/news/<bse>')
def news(bse):
    try:
        s = requests.Session()
        s.headers.update({'User-Agent': 'Mozilla/5.0'})
        url = f'https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?strCat=-1&strPrevDate=&strScrip={bse}&strSearch=P&strToDate=&strType=C&subcategory=-1'
        r = s.get(url, timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
