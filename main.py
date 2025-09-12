<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>ForexPro — Structure Scanner</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg: #0b0f14;
      --card: #111827;
      --muted: #3a3b46;
      --text: #e5e5e5;
      --accent: #22c55e;
      --danger: #ef4444;
      --btn: #1f2937;
      --btn-bd: #334155;
    }
    body {
      margin: 0;
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 15px;
      border-bottom: 1px solid var(--muted);
    }
    header img {
      height: 40px;
    }
    header h1 {
      font-size: 1.2rem;
      font-weight: 700;
      margin: 0;
    }
    main {
      padding: 20px;
      max-width: 900px;
      margin: 0 auto;
    }
    .form-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 10px;
    }
    input[type=text] {
      background: var(--card);
      border: 1px solid var(--btn-bd);
      color: var(--text);
      padding: 8px 10px;
      border-radius: 6px;
      flex: 1;
      min-width: 200px;
    }
    button {
      background: var(--btn);
      border: 1px solid var(--btn-bd);
      border-radius: 6px;
      padding: 8px 12px;
      color: var(--text);
      cursor: pointer;
    }
    button:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .check-group {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }
    label {
      display: flex;
      align-items: center;
      gap: 4px;
    }
    #results {
      margin-top: 20px;
    }
    .card {
      background: var(--card);
      border-radius: 8px;
      padding: 15px;
      margin-bottom: 20px;
    }
    h3 {
      margin: 0 0 5px 0;
      font-size: 1.1rem;
    }
    h4 {
      margin: 10px 0 4px;
      font-size: 0.9rem;
      font-weight: 600;
    }
    .line-items {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .pill {
      background: var(--muted);
      padding: 4px 8px;
      border-radius: 6px;
      font-size: 0.8rem;
    }
    .pill.bull {
      background: #14532d;
      color: #bbf7d0;
    }
    .pill.bear {
      background: #7f1d1d;
      color: #fecaca;
    }
    .badge {
      background: #334155;
      border-radius: 4px;
      padding: 2px 4px;
      margin-right: 4px;
      font-size: 0.7rem;
    }
    .empty {
      color: #666;
      font-size: 0.8rem;
    }
    .summary {
      margin-bottom: 15px;
      font-weight: 600;
    }
    .info {
      padding: 8px;
      background: #1e293b;
      border-radius: 6px;
      font-size: 0.85rem;
      margin-bottom: 8px;
    }
    .error {
      padding: 8px;
      background: #450a0a;
      border-radius: 6px;
      font-size: 0.8rem;
      white-space: pre-wrap;
      color: #fecaca;
    }
  </style>
</head>
<body>
  <header>
    <img src="logo.png" alt="ForexPro Logo" />
    <h1>ForexPro — Structure Scanner</h1>
  </header>
  <main>
    <div class="form-row">
      <input type="text" id="backendUrl" placeholder="Backend URL เช่น https://xau-scanner.onrender.com" />
      <input type="text" id="symbolInput" placeholder="Symbol เช่น XAUUSD" />
    </div>
    <div class="check-group">
      <label><input type="checkbox" id="tfM5" checked /> M5</label>
      <label><input type="checkbox" id="tfM15" checked /> M15</label>
      <label><input type="checkbox" id="tfM30" checked /> M30</label>
      <label><input type="checkbox" id="tfH1" checked /> H1</label>
      <label><input type="checkbox" id="tfH4" checked /> H4</label>
      <label><input type="checkbox" id="tfD1" checked /> D1</label>
    </div>
    <div class="form-row">
      <button id="btnSaveUrl">💾 บันทึก URL</button>
      <button id="btnScan">🔎 สแกนโครงสร้าง</button>
    </div>
    <div id="results"></div>
  </main>
  <script src="app.js"></script>
</body>
</html>
