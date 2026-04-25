from flask import Flask, jsonify, request
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'application/json',
}

@app.route('/price/<symbol>')
def price(symbol):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS?range=max&interval=1mo&events=div,splits'
    r = requests.get(url, headers=HEADERS, timeout=10)
    return jsonify(r.json())

@app.route('/fundamentals/<symbol>')
def fundamentals(symbol):
    url = f'https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}.NS?modules=financialData,defaultKeyStatistics,summaryDetail,incomeStatementHistory,balanceSheetHistory,cashflowStatementHistory'
    r = requests.get(url, headers=HEADERS, timeout=10)
    return jsonify(r.json())

@app.route('/news/<bse>')
def news(bse):
    url = f'https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?strCat=-1&strPrevDate=&strScrip={bse}&strSearch=P&strToDate=&strType=C&subcategory=-1'
    r = requests.get(url, headers=HEADERS, timeout=10)
    return jsonify(r.json())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
