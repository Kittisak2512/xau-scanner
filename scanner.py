
import pandas as pd
import numpy as np

# ===== Utilities =====
def to_datetime_index(df):
    df = df.copy()
    df['time'] = pd.to_datetime(df['time'])
    return df.set_index('time', drop=False).sort_index()

def resample_ohlc(df, rule):
    o = df['open'].resample(rule).first()
    h = df['high'].resample(rule).max()
    l = df['low'].resample(rule).min()
    c = df['close'].resample(rule).last()
    out = pd.DataFrame({'time':o.index, 'open':o.values, 'high':h.values, 'low':l.values, 'close':c.values})
    return out.dropna()

def atr(df, n=14):
    h, l, c = df['high'], df['low'], df['close']
    tr = np.maximum(h - l, np.maximum((h - c.shift(1)).abs(), (l - c.shift(1)).abs()))
    return pd.Series(tr).rolling(n).mean()

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def body_ratio(row):
    rng = row['high'] - row['low']
    if rng == 0: return 0.0
    return abs(row['close'] - row['open']) / rng

def find_zones_h1(h1: pd.DataFrame, swing_lookback=5, zone_pad_ratio=0.2):
    d = h1.copy()
    d['ATR'] = atr(d, 14)
    d['swing_high'] = (d['high'] == d['high'].rolling(swing_lookback, center=True).max())
    d['swing_low']  = (d['low']  == d['low'].rolling(swing_lookback, center=True).min())
    piv_hi = d[d['swing_high']].tail(5)
    piv_lo = d[d['swing_low']].tail(5)
    if piv_hi.empty or piv_lo.empty:
        return None, None, d
    atr_last = d['ATR'].iloc[-1] if len(d)>0 else np.nan
    pad = (zone_pad_ratio * atr_last) if pd.notna(atr_last) else 0.5
    z_up_mid = piv_hi['high'].iloc[-1]
    z_dn_mid = piv_lo['low'].iloc[-1]
    upper_zone = (z_up_mid - pad, z_up_mid + pad)
    lower_zone = (z_dn_mid - pad, z_dn_mid + pad)
    return upper_zone, lower_zone, d

def is_breakout_m15(m15_row_close, zone, direction, buffer_pts):
    low, high = zone
    if direction == 'up':
        return m15_row_close > high + buffer_pts
    else:
        return m15_row_close < low - buffer_pts

def touched_zone(lo, hi, zone, tol):
    low, high = zone
    return not (hi < low - tol or lo > high + tol)

def confirm_pattern_m5(df_m5, idx, direction, atr_val):
    if idx < 1: return None
    row = df_m5.iloc[idx]
    prev = df_m5.iloc[idx-1]
    body = abs(row['close'] - row['open'])
    rng  = row['high'] - row['low']
    if rng <= 0: return None
    body_r = body / rng

    # Engulfing
    prev_low  = min(prev['open'], prev['close'])
    prev_high = max(prev['open'], prev['close'])
    cur_low   = min(row['open'], row['close'])
    cur_high  = max(row['open'], row['close'])
    engulf_ok = (cur_low <= prev_low) and (cur_high >= prev_high) and (body >= 0.4*atr_val)
    if engulf_ok:
        if direction=='up' and row['close']>row['open']: return 'Engulfing(M5)'
        if direction=='down' and row['close']<row['open']: return 'Engulfing(M5)'

    # Pin Bar
    upper_w = row['high'] - max(row['open'], row['close'])
    lower_w = min(row['open'], row['close']) - row['low']
    if body>0 and (upper_w >= 2*body or lower_w >= 2*body):
        close_pos = (row['close'] - row['low'])/rng
        if direction=='up' and close_pos>=0.70: return 'PinBar(M5)'
        if direction=='down' and close_pos<=0.30: return 'PinBar(M5)'

    # Marubozu
    if body_r >= 0.8:
        if direction=='up' and row['close']>row['open']: return 'Marubozu(M5)'
        if direction=='down' and row['close']<row['open']: return 'Marubozu(M5)'
    return None

def scan_m15_breakout_m5_confirm(df_m5: pd.DataFrame,
                                 retest_m5_window=24,
                                 sl_after_zone=12,
                                 tp1_pts=25, tp2_pts=50,
                                 body_ratio_filter=0.6):
    if df_m5.empty:
        return {"status": "NO_DATA", "reason": ["ไม่มีข้อมูล"]}

    d5 = df_m5.copy()
    d5.columns = [c.strip().lower() for c in d5.columns]
    required = {'time','open','high','low','close'}
    if not required.issubset(set(d5.columns)):
        return {"status":"ERROR","reason":[f"คอลัมน์ต้องมี {required}"]}
    d5 = to_datetime_index(d5)
    d5 = d5.sort_index()

    d15 = resample_ohlc(d5, '15min')
    d60 = resample_ohlc(d5, '60min')
    if len(d15)<30 or len(d60)<10:
        return {"status":"NO_SETUP","reason":["ข้อมูล M5 น้อยเกินไป"]}

    d15['ATR'] = atr(d15, 14)
    d60['EMA50'] = ema(d60['close'], 50)

    upper_zone, lower_zone, _ = find_zones_h1(d60)
    if upper_zone is None:
        return {"status":"NO_SETUP","reason":["หาโซน H1 ไม่ได้"]}

    trend_up = len(d60)>=5 and (d60['close'].iloc[-1] > d60['EMA50'].iloc[-1]) and (d60['EMA50'].iloc[-1] - d60['EMA50'].iloc[-5] > 0)
    trend_dn = len(d60)>=5 and (d60['close'].iloc[-1] < d60['EMA50'].iloc[-1]) and (d60['EMA50'].iloc[-1] - d60['EMA50'].iloc[-5] < 0)

    buffer_pts = max(10, 0.15*(d15['ATR'].iloc[-1]))
    N = 40
    breakout = None
    for i in range(max(1, len(d15)-N), len(d15)-1):
        row = d15.iloc[i]
        b_ratio = body_ratio(row)
        if trend_up and b_ratio>=body_ratio_filter and is_breakout_m15(row['close'], upper_zone, 'up', buffer_pts):
            breakout = ('up', i, upper_zone)
        if trend_dn and b_ratio>=body_ratio_filter and is_breakout_m15(row['close'], lower_zone, 'down', buffer_pts):
            breakout = ('down', i, lower_zone)
    if breakout is None:
        return {"status":"WATCH","reason":["ยังไม่มี M15 ปิดทะลุโซนพร้อมเทรนด์สนับสนุน"]}

    direction, i_brk, zone = breakout
    brk_time = d15.iloc[i_brk]['time']

    d5_after = d5[d5['time'] >= brk_time].copy().iloc[:retest_m5_window]
    if d5_after.empty:
        return {"status":"WAIT_DATA","reason":["ยังไม่มี M5 หลัง breakout มากพอ"]}

    d5_after['ATR5'] = atr(d5_after, 14)
    conf_idx, conf_type, retest_time = None, None, None
    for j in range(len(d5_after)):
        lo, hi = d5_after.iloc[j]['low'], d5_after.iloc[j]['high']
        tol = 0.1 * (d5_after['ATR5'].iloc[j] if pd.notna(d5_after['ATR5'].iloc[j]) else 5)
        if touched_zone(lo, hi, zone, tol):
            for k in range(j, min(j+4, len(d5_after))):
                ctype = confirm_pattern_m5(d5_after, k, direction, d5_after['ATR5'].iloc[k] if 'ATR5' in d5_after else 0)
                if ctype:
                    conf_idx = k; conf_type = ctype; retest_time = d5_after.iloc[j]['time']
                    break
            if conf_idx is not None: break

    if conf_idx is None:
        return {"status":"WATCH","reason":["แตะโซนแล้ว แต่ยังไม่เห็นแท่งยืนยันบน M5"],
                "meta":{"breakout_time":str(brk_time)}}

    entry_row = d5_after.iloc[conf_idx]
    entry = float(entry_row['close'])
    z_low, z_high = zone
    if direction=='up':
        sl  = z_low - sl_after_zone
        tp1 = entry + tp1_pts
        tp2 = entry + tp2_pts
        status = "BUY"
    else:
        sl  = z_high + sl_after_zone
        tp1 = entry - tp1_pts
        tp2 = entry - tp2_pts
        status = "SELL"

    return {
        "status": status,
        "direction": direction.upper(),
        "h1_zone": [round(z_low,2), round(z_high,2)],
        "breakout_m15_time": str(brk_time),
        "retest_m5_time": str(retest_time),
        "confirm_m5_time": str(entry_row['time']),
        "confirm_type": conf_type,
        "entry": round(entry,2),
        "sl": round(sl,2),
        "tp1": round(tp1,2),
        "tp2": round(tp2,2)
    }
