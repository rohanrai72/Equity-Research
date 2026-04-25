from flask import Flask, jsonify
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
import time
import csv
import io

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
        # Stooq serves Indian NSE stocks as SYMBOL.NS
        url = f'https://stooq.com/q/d/l/?s={symbol.lower()}.ns&i=m'
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        if r.status_code != 200 or 'Date' not in r.text:
            return jsonify({'error': 'No data', 'raw': r.text[:200]}), 404
        reader = csv.DictReader(io.StringIO(r.text))
        data = [{'date': row['Date'], 'close': float(row['Close'])} 
                for row in reader if row.get('Close') and row['Close'] != 'null']
        return jsonify({'prices': data})
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
