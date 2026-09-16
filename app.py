#!/usr/bin/env python3
"""
Mock Tipalti — a stand-in Tipalti AP/Bills UI + SOAP endpoint for the AOSN demo.

Modelled on how Tipalti actually structures a bill (its real CreateOrUpdateInvoices API):
- a payee (Idap), invoice number/date, currency, and an InvoiceStatus that moves
  PendingApReview -> PendingApproval -> PendingPayment -> Paid;
- InvoiceLines, each with a LineType of Item-based (a PO line, carrying a
  RelatedPurchaseOrder.PurchaseOrderNumber) or Account-based (freight, tax — no PO),
  a GL account and a TaxAmount.
This is what answers the SE questions on Kris's email: where the PO number, the line
items, and freight/tax (as non-PO lines) are passed, how PO matching / approval / payment
are represented, and how exceptions (PO not found, duplicate, tolerance, freight/tax) show.

Stdlib only. Run:  python3 app.py   (serves on :5055)
Expose:  ngrok http 5055  ->  set the agent's TIPALTI_MOCK_URL to
    https://<subdomain>.ngrok-free.app/soap
"""
import json
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 5055
_LOCK = threading.Lock()
_BILLS = []          # newest first
_SEQ = {"n": 1000}


def _text(xml, tag):
    m = re.search(r"<%s\b[^>]*>(.*?)</%s>" % (tag, tag), xml, re.S | re.I)
    return (m.group(1).strip() if m else "")


def _num(x):
    try:
        return round(float(str(x).replace(",", "").replace("$", "") or 0), 2)
    except ValueError:
        return 0.0


def _parse_soap(xml):
    lines = []
    for block in re.findall(r"<Line\b[^>]*>(.*?)</Line>", xml, re.S | re.I):
        lines.append({
            "description": _text(block, "Description"),
            "amount": _num(_text(block, "Amount")),
            "type": (_text(block, "Type") or "").lower(),
            "po_number": _text(block, "PONumber"),
        })
    return {
        "vendor_name": _text(xml, "VendorName"),
        "invoice_number": _text(xml, "InvoiceNumber"),
        "po_number": _text(xml, "PONumber"),
        "currency": _text(xml, "Currency") or "USD",
        "total": _num(_text(xml, "Total")),
        "lines": lines,
    }


def _gl_for(desc, is_po_line):
    d = (desc or "").lower()
    if not is_po_line:
        if "tax" in d:
            return {"number": "2200", "name": "Sales Tax Payable", "type": "Liability"}
        if "freight" in d or "ship" in d or "delivery" in d or "handling" in d:
            return {"number": "6100", "name": "Freight & Delivery", "type": "Expense"}
        return {"number": "6900", "name": "Other Charges", "type": "Expense"}
    return {"number": "5000", "name": "Medical Supplies — COGS", "type": "Expense"}


def _enrich(fields):
    """Turn the raw SOAP into a Tipalti-shaped bill, with derived workflow + checks."""
    lines = []
    goods = tax = freight = 0.0
    for li in fields["lines"]:
        is_po = (li["type"] == "po_line") or (bool(li["po_number"]) and li["type"] != "non_po_line")
        gl = _gl_for(li["description"], is_po)
        amt = _num(li["amount"])
        d = (li["description"] or "").lower()
        if is_po:
            goods += amt
        elif "tax" in d:
            tax += amt
        else:
            freight += amt
        lines.append({
            "description": li["description"],
            "amount": amt,
            "line_type": "Item-based" if is_po else "Account-based",
            "is_po": is_po,
            "po_number": li["po_number"] if is_po else "",
            "gl": gl,
            "tax_amount": amt if (not is_po and "tax" in d) else 0.0,
            "match": "3-way matched" if is_po else "—",
        })

    total = _num(fields["total"]) or round(goods + freight + tax, 2)
    computed = round(goods + freight + tax, 2)
    header_po = fields["po_number"] or next((l["po_number"] for l in lines if l["is_po"]), "")

    # duplicate check against already-stored bills
    dup = any(b["invoice_number"] == fields["invoice_number"] and b["payee"] == fields["vendor_name"]
              for b in _BILLS)

    has_po = bool(header_po) or any(l["is_po"] for l in lines)
    ties = abs(computed - total) < 0.01

    if dup:
        status, stage = "Duplicate — review", 0
    elif not has_po:
        status, stage = "Pending AP Review", 0
    elif not ties:
        status, stage = "On hold — amount tolerance", 1
    else:
        status, stage = "Pending Approval", 2   # PO-matched, awaiting approver

    checks = [
        {"label": "PO number present & matched", "ok": has_po,
         "note": ("3-way match: PO + receipt + invoice" if has_po else "No PO on invoice — routed to AP review")},
        {"label": "Line amounts within tolerance", "ok": ties,
         "note": ("lines + freight + tax = %s" % _money(total) if ties else "off by %s" % _money(round(computed - total, 2)))},
        {"label": "Freight & tax on their own non-PO lines", "ok": (freight > 0 or tax > 0),
         "note": ("freight %s · tax %s (Account-based)" % (_money(freight), _money(tax)))},
        {"label": "Not a duplicate", "ok": (not dup),
         "note": ("first time seen" if not dup else "same payee + invoice number already in queue")},
    ]

    now = datetime.now()
    return {
        "payee": fields["vendor_name"],
        "invoice_number": fields["invoice_number"],
        "po_number": header_po,
        "currency": fields["currency"],
        "total": total,
        "goods": round(goods, 2), "freight": round(freight, 2), "tax": round(tax, 2),
        "lines": lines,
        "status": status, "stage": stage,
        "subsidiary": "AOS Network, Inc.",
        "location": "Central AP — Newport Beach, CA",
        "payment_method": "ACH · Net 30",
        "invoice_date": now.strftime("%b %d, %Y"),
        "due_date": (now + timedelta(days=30)).strftime("%b %d, %Y"),
        "checks": checks,
    }


def _money(x):
    return "$%s" % format(_num(x), ",.2f")


def _store_bill(fields):
    with _LOCK:
        _SEQ["n"] += 1
        bill = _enrich(fields)
        bill["bill_id"] = "BILL-%d" % _SEQ["n"]
        bill["received_at"] = datetime.now().strftime("%b %d, %Y %H:%M")
        _BILLS.insert(0, bill)
        return bill["bill_id"]


def _soap_response(bill_id):
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        '<CreateOrUpdateInvoicesResult xmlns="http://tipalti.com/">'
        "<Ok>true</Ok>"
        f"<BillId>{bill_id}</BillId>"
        "<InvoiceStatus>PendingApproval</InvoiceStatus>"
        "</CreateOrUpdateInvoicesResult>"
        "</soap:Body></soap:Envelope>"
    ).encode("utf-8")


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tipalti — Bills</title>
<style>
  :root{ --navy:#0f1e38; --navy2:#16294a; --ink:#1b2437; --gold:#fdb913;
         --line:#e7eaf0; --muted:#6b7488; --bg:#f4f6fa; --blue:#3b4ce0;
         --green:#2fae66; --greenbg:#e4f7ec; --greybg:#eef0f4; --amber:#9a6b00;
         --amberbg:#fbeecb; --redbg:#fbe4e4; --red:#c0392b; --bluebg:#eef1ff; }
  *{box-sizing:border-box} html,body{margin:0}
  body{font-family:'Segoe UI',system-ui,Arial,sans-serif;background:var(--bg);color:var(--ink);font-size:14px}
  .app{display:flex;min-height:100vh}
  .side{width:216px;background:var(--navy);color:#c9d2e3;flex:0 0 216px;display:flex;flex-direction:column}
  .logo{padding:20px 22px 18px;font-size:24px;font-weight:800;letter-spacing:-1px;color:#fff}
  .logo .swoosh{color:var(--gold)}
  .nav a{display:flex;gap:10px;align-items:center;padding:11px 22px;color:#c9d2e3;text-decoration:none;font-size:13.5px}
  .nav a .ico{width:16px;text-align:center;opacity:.85}
  .nav a.active{background:var(--blue);color:#fff;font-weight:600}
  .nav a:hover:not(.active){background:var(--navy2);color:#fff}
  .side .foot{margin-top:auto;padding:16px 22px;font-size:11px;color:#7f8aa3}
  .main{flex:1;min-width:0;display:flex;flex-direction:column}
  .top{height:56px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 24px}
  .top .crumb{font-weight:700;font-size:16px} .top .who{color:var(--muted);font-size:13px}
  .wrap{padding:22px 24px}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}
  .card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .card .k{color:var(--muted);font-size:12px} .card .v{font-size:20px;font-weight:800;margin-top:4px}
  .card .v.gold{color:#b9860b}
  .panel{background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}
  .panel h2{margin:0;padding:14px 16px;font-size:15px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
  .panel h2 .live{font-size:11px;color:var(--muted);font-weight:500}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);padding:10px 16px;border-bottom:1px solid var(--line)}
  td{padding:12px 16px;border-bottom:1px solid var(--line);vertical-align:top}
  tr:last-child td{border-bottom:0}
  tbody.rows tr{cursor:pointer} tbody.rows tr:hover{background:#f7f9fd}
  .r{text-align:right} .mono{font-variant-numeric:tabular-nums}
  .vend{font-weight:600} .sub{color:var(--muted);font-size:12px}
  .po{display:inline-block;background:var(--bluebg);color:var(--blue);border-radius:5px;padding:1px 7px;font-size:12px;font-weight:600}
  .po.none{background:#f3f4f6;color:#98a0ae}
  .badge{display:inline-block;border-radius:999px;padding:3px 11px;font-size:12px;font-weight:700;white-space:nowrap}
  .b-approval{background:var(--bluebg);color:var(--blue)} .b-payment{background:var(--amberbg);color:var(--amber)}
  .b-paid{background:var(--greenbg);color:var(--green)} .b-review{background:var(--greybg);color:#5b6474}
  .b-hold{background:var(--amberbg);color:var(--amber)} .b-dup{background:var(--redbg);color:var(--red)}
  .empty{padding:40px;text-align:center;color:var(--muted)}
  /* drawer */
  .scrim{position:fixed;inset:0;background:rgba(15,30,56,.35);display:none}
  .scrim.on{display:block}
  .drawer{position:fixed;top:0;right:0;height:100vh;width:560px;max-width:94vw;background:#fff;
    box-shadow:-8px 0 30px rgba(0,0,0,.18);transform:translateX(100%);transition:transform .18s ease;overflow-y:auto}
  .drawer.on{transform:translateX(0)}
  .dhead{display:flex;justify-content:space-between;align-items:flex-start;padding:18px 22px;border-bottom:1px solid var(--line);position:sticky;top:0;background:#fff}
  .dhead h3{margin:0;font-size:17px} .dhead .x{cursor:pointer;color:var(--muted);font-size:22px;line-height:1}
  .dbody{padding:18px 22px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px 18px;margin-bottom:8px}
  .f .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.3px}
  .f .v{font-size:14px;font-weight:600;margin-top:2px}
  .stepper{display:flex;gap:0;margin:16px 0 20px}
  .step{flex:1;text-align:center;font-size:11px;color:var(--muted);position:relative}
  .step .dot{width:22px;height:22px;border-radius:50%;background:#e2e6ee;color:#8a93a6;margin:0 auto 6px;
    display:flex;align-items:center;justify-content:center;font-weight:700;font-size:12px}
  .step.done .dot{background:var(--green);color:#fff} .step.cur .dot{background:var(--blue);color:#fff}
  .step::before{content:"";position:absolute;top:11px;left:-50%;width:100%;height:2px;background:#e2e6ee;z-index:-0}
  .step:first-child::before{display:none} .step.done::before,.step.cur::before{background:#bcd0ff}
  h4{margin:18px 0 8px;font-size:13px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted)}
  .lt{display:inline-block;font-size:11px;padding:1px 6px;border-radius:4px;font-weight:600}
  .lt.item{background:var(--bluebg);color:var(--blue)} .lt.acct{background:#fff3d6;color:#8a6100}
  table.det td,table.det th{padding:7px 8px;font-size:12.5px} table.det th{border-bottom:1px solid var(--line)}
  table.det td{border-bottom:1px solid #f0f2f6}
  .chk{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;border:1px solid var(--line);border-radius:8px;margin-bottom:7px}
  .chk .cl{font-weight:600;font-size:13px} .chk .cn{font-size:12px;color:var(--muted)}
  .pill{font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px}
  .pill.pass{background:var(--greenbg);color:var(--green)} .pill.flag{background:var(--amberbg);color:var(--amber)}
  .totrow{display:flex;justify-content:space-between;font-size:13px;padding:2px 0} .totrow.grand{font-weight:800;border-top:1.5px solid var(--ink);margin-top:5px;padding-top:6px}
</style></head>
<body>
<div class="app">
  <aside class="side">
    <div class="logo"><span class="swoosh">◜</span> tipalti</div>
    <nav class="nav">
      <a href="#"><span class="ico">▦</span> Home</a>
      <a href="#" class="active"><span class="ico">▤</span> Bills</a>
      <a href="#"><span class="ico">▣</span> Payments</a>
      <a href="#"><span class="ico">◇</span> Payees</a>
      <a href="#"><span class="ico">◈</span> Detect</a>
      <a href="#"><span class="ico">▧</span> Documents</a>
      <a href="#"><span class="ico">⚙</span> Administration</a>
    </nav>
    <div class="foot">Contact · Privacy · Terms of use<br>© Tipalti (mock)</div>
  </aside>
  <div class="main">
    <div class="top"><div class="crumb">Bills</div><div class="who">AOS Network · Central AP · Newport Beach</div></div>
    <div class="wrap">
      <div class="cards">
        <div class="card"><div class="k">Bills received</div><div class="v" id="c-count">0</div></div>
        <div class="card"><div class="k">Total value</div><div class="v gold" id="c-total">$0.00</div></div>
        <div class="card"><div class="k">Pending approval</div><div class="v" id="c-pending">0</div></div>
        <div class="card"><div class="k">Need review</div><div class="v" id="c-review">0</div></div>
      </div>
      <div class="panel">
        <h2>Bills queue <span class="live">● live — refreshes automatically · click a bill to open it</span></h2>
        <table>
          <thead><tr><th>Payee</th><th>Invoice</th><th>PO</th><th>Lines</th><th class="r">Amount</th><th>Status</th><th>Received</th></tr></thead>
          <tbody class="rows" id="rows"><tr><td colspan="7" class="empty">Waiting for the agent to post a bill…</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="scrim" id="scrim" onclick="closeDrawer()"></div>
<div class="drawer" id="drawer"><div id="drawer-content"></div></div>

<script>
const money = n => "$" + Number(n||0).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const STAGES = ["AP Review","PO Match","Approval","Payment"];
function badgeClass(s){ s=(s||"").toLowerCase();
  if(s.includes("paid"))return"b-paid"; if(s.includes("approval"))return"b-approval";
  if(s.includes("payment"))return"b-payment"; if(s.includes("duplicate"))return"b-dup";
  if(s.includes("hold")||s.includes("tolerance"))return"b-hold"; return"b-review"; }
let BILLS=[];
function render(bills){
  BILLS=bills;
  const tb=document.getElementById("rows");
  document.getElementById("c-count").textContent=bills.length;
  document.getElementById("c-pending").textContent=bills.filter(b=>(b.status||"").includes("Approval")).length;
  document.getElementById("c-review").textContent=bills.filter(b=>/review|hold|duplicate|tolerance/i.test(b.status||"")).length;
  document.getElementById("c-total").textContent=money(bills.reduce((s,b)=>s+Number(b.total||0),0));
  if(!bills.length){tb.innerHTML='<tr><td colspan="7" class="empty">Waiting for the agent to post a bill…</td></tr>';return;}
  tb.innerHTML=bills.map((b,i)=>{
    const po=b.po_number?'<span class="po">'+b.po_number+'</span>':'<span class="po none">no PO</span>';
    const cnt=(b.lines||[]).length;
    return '<tr onclick="openDrawer('+i+')">'+
      '<td><div class="vend">'+(b.payee||'—')+'</div><div class="sub">'+b.bill_id+' · '+(b.subsidiary||'')+'</div></td>'+
      '<td class="mono">'+(b.invoice_number||'—')+'</td><td>'+po+'</td>'+
      '<td>'+cnt+'</td><td class="r mono">'+money(b.total)+' <span class="sub">'+(b.currency||'USD')+'</span></td>'+
      '<td><span class="badge '+badgeClass(b.status)+'">'+(b.status||'')+'</span></td>'+
      '<td class="sub">'+(b.received_at||'')+'</td></tr>';
  }).join("");
}
function openDrawer(i){
  const b=BILLS[i]; if(!b)return;
  const stepHtml=STAGES.map((s,idx)=>{
    const cls=idx<b.stage?'done':(idx===b.stage?'cur':'');
    return '<div class="step '+cls+'"><div class="dot">'+(idx<b.stage?'✓':(idx+1))+'</div>'+s+'</div>';
  }).join("");
  const lineRows=(b.lines||[]).map(l=>{
    const lt=l.is_po?'<span class="lt item">Item-based</span>':'<span class="lt acct">Account-based</span>';
    const po=l.is_po?'<span class="po">'+(l.po_number||'—')+'</span>':'<span class="sub">— non-PO</span>';
    return '<tr><td>'+l.description+'<div class="sub">GL '+l.gl.number+' · '+l.gl.name+'</div></td>'+
      '<td>'+lt+'</td><td>'+po+'</td><td class="r mono">'+money(l.amount)+'</td>'+
      '<td class="r mono">'+(l.tax_amount?money(l.tax_amount):'—')+'</td>'+
      '<td class="sub">'+l.match+'</td></tr>';
  }).join("");
  const checks=(b.checks||[]).map(c=>
    '<div class="chk"><div><div class="cl">'+c.label+'</div><div class="cn">'+c.note+'</div></div>'+
    '<span class="pill '+(c.ok?'pass':'flag')+'">'+(c.ok?'Pass':'Review')+'</span></div>').join("");
  const f=(k,v)=>'<div class="f"><div class="k">'+k+'</div><div class="v">'+(v||'—')+'</div></div>';
  document.getElementById("drawer-content").innerHTML=
    '<div class="dhead"><div><h3>'+(b.payee||'')+' · '+(b.invoice_number||'')+'</h3>'+
      '<div class="sub">'+b.bill_id+' · <span class="badge '+badgeClass(b.status)+'">'+b.status+'</span></div></div>'+
      '<div class="x" onclick="closeDrawer()">×</div></div>'+
    '<div class="dbody">'+
      '<div class="stepper">'+stepHtml+'</div>'+
      '<div class="grid2">'+
        f("Payee (Idap)",b.payee)+f("PO number",b.po_number||"— none")+
        f("Invoice #",b.invoice_number)+f("Currency",b.currency)+
        f("Invoice date",b.invoice_date)+f("Due date",b.due_date)+
        f("Subsidiary",b.subsidiary)+f("Location",b.location)+
        f("Payment method",b.payment_method)+f("Bill total",money(b.total))+
      '</div>'+
      '<h4>Invoice lines — Item-based (PO) vs Account-based (freight / tax)</h4>'+
      '<table class="det"><thead><tr><th>Description / GL</th><th>Line type</th><th>PO #</th><th class="r">Amount</th><th class="r">Tax</th><th>Match</th></tr></thead><tbody>'+lineRows+'</tbody></table>'+
      '<div style="margin-top:10px">'+
        '<div class="totrow"><span class="sub">Goods (PO lines)</span><span class="mono">'+money(b.goods)+'</span></div>'+
        '<div class="totrow"><span class="sub">Freight (non-PO)</span><span class="mono">'+money(b.freight)+'</span></div>'+
        '<div class="totrow"><span class="sub">Tax (non-PO)</span><span class="mono">'+money(b.tax)+'</span></div>'+
        '<div class="totrow grand"><span>Bill total</span><span class="mono">'+money(b.total)+'</span></div>'+
      '</div>'+
      '<h4>Validation & exceptions</h4>'+checks+
    '</div>';
  document.getElementById("drawer").classList.add("on");
  document.getElementById("scrim").classList.add("on");
}
function closeDrawer(){document.getElementById("drawer").classList.remove("on");document.getElementById("scrim").classList.remove("on");}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeDrawer();});
async function tick(){ try{ const r=await fetch("/api/bills",{cache:"no-store"}); render(await r.json()); }catch(e){} }
tick(); setInterval(tick,2000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/bills":
            with _LOCK:
                body = json.dumps(_BILLS).encode("utf-8")
            self._send(200, body, "application/json")
        elif path == "/reset":
            with _LOCK:
                _BILLS.clear()
            self._send(200, b'{"ok":true,"message":"bills cleared"}', "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        path = self.path.split("?")[0]
        if path in ("/soap", "/"):
            fields = _parse_soap(raw)
            bill_id = _store_bill(fields)
            print("[mock-tipalti] stored %s  %s  %s  total=%s  lines=%d"
                  % (bill_id, fields["vendor_name"], fields["invoice_number"],
                     fields["total"], len(fields["lines"])))
            self._send(200, _soap_response(bill_id), "text/xml; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        raise SystemExit(
            "Could not bind port %d (%s).\n"
            "A copy is probably already running — use it, or free the port with:\n"
            "  lsof -ti tcp:%d | xargs kill" % (PORT, e, PORT))
    print("Mock Tipalti on http://localhost:%d  (UI: /  ·  SOAP: /soap  ·  reset: /reset)" % PORT)
    srv.serve_forever()
