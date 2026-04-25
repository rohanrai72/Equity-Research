from flask import Flask, jsonify, request
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://finance.yahoo.com',
    'Origin': 'https://finance.yahoo.com',
})

def get_crumb():
    SESSION.get('https://finance.yahoo.com', timeout=10)
    r = SESSION.get('https://query1.finance.yahoo.com/v1/test/getcrumb', timeout=10)
    return r.text.strip()

CRUMB = None

def crumb():
    global CRUMB
    if not CRUMB:
        CRUMB = get_crumb()
    return CRUMB

@app.route('/price/<symbol>')
def price(symbol):
    try:
        c = crumb()
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS?range=max&interval=1mo&events=div,splits&crumb={c}'
        r = SESSION.get(url, timeout=15)
        if r.status_code == 401:
            global CRUMB
            CRUMB = get_crumb()
            r = SESSION.get(url.replace(c, CRUMB), timeout=15)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/fundamentals/<symbol>')
def fundamentals(symbol):
    try:
        c = crumb()
        url = f'https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}.NS?modules=financialData,defaultKeyStatistics,summaryDetail,incomeStatementHistory,balanceSheetHistory,cashflowStatementHistory&crumb={c}'
        r = SESSION.get(url, timeout=15)
        if r.status_code == 401:
            global CRUMB
            CRUMB = get_crumb()
            r = SESSION.get(url.replace(c, CRUMB), timeout=15)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/news/<bse>')
def news(bse):
    try:
        url = f'https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?strCat=-1&strPrevDate=&strScrip={bse}&strSearch=P&strToDate=&strType=C&subcategory=-1'
        r = SESSION.get(url, timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
