@app.route('/price/<symbol>')
def price(symbol):
    try:
        s = nse_session()
        from datetime import datetime, timedelta
        end = datetime.now().strftime('%d-%m-%Y')
        start = (datetime.now() - timedelta(days=365*25)).strftime('%d-%m-%Y')
        url = f'https://www.nseindia.com/api/historical/cm/equity?symbol={symbol}&series=["EQ"]&from={start}&to={end}&csv=false'
        r = s.get(url, timeout=15)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500
