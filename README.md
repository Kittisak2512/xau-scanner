# XAU Scanner API (FastAPI)

Minimal backend สำหรับสแกนภาพกราฟ 2 โหมด:
- `POST /scan-image` — วิเคราะห์แนวโน้มจาก **ภาพเดียว** (heuristic จากความสว่าง)
- `POST /scan-breakout` — วิเคราะห์ **Breakout + Retest** จาก **2 ภาพ** (Higher TF สร้างกรอบ / Lower TF ดู last)

> **หมายเหตุ:** อัลกอริทึมเป็น heuristic ไว้ใช้งานเบาๆ ไม่อิงราคาจริงจากโบรกเกอร์  
> ใช้เปรียบเทียบเชิงสัมพัทธ์ว่า last อยู่ใน/นอกกรอบ และอยู่ในโซนรีเทสหรือไม่

## Run local
```bash
pip install -r requirements.txt
uvicorn main:app --reload
