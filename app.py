from flask import Flask, jsonify
from flask_cors import CORS
import yfinance as yf

app = Flask(__name__)
CORS(app)

@app.route('/price/<symbol>')
def price(symbol):
    try:
        t = yf.Ticker(f"{symbol}.NS")
        hist = t.history(period="max")
        data = []
        for date, row in hist.iterrows():
            data.append({'date': str(date.date()), 'close': round(row['Close'], 2)})
        return jsonify({'prices': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/fundamentals/<symbol>')
def fundamentals(symbol):
    try:
        t = yf.Ticker(f"{symbol}.NS")
        info = t.info
        bs = t.balance_sheet
        income = t.income_stmt
        cf = t.cashflow
        def tbl(df):
            if df is None or df.empty:
                return {}
            df.index = df.index.astype(str)
            df.columns = [str(c.date()) for c in df.columns]
            return df.fillna(0).astype(float).to_dict()
        return jsonify({
            'info': info,
            'balanceSheet': tbl(bs),
            'incomeStmt': tbl(income),
            'cashflow': tbl(cf)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/news/<bse>')
def news(bse):
    try:
        import requests
        url = f'https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?strCat=-1&strPrevDate=&strScrip={bse}&strSearch=P&strToDate=&strType=C&subcategory=-1'
        r = requests.get(url, headers={'User-Agent':'Mozilla/5.0'}, timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
