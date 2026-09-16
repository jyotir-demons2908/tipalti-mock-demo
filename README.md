# Mock Tipalti — AOSN demo

A stand-in Tipalti bill-pay UI + SOAP endpoint. The agent's **Tipalti Post** step posts a
SOAP bill here; it appears in a Tipalti-styled **Bills queue** that refreshes live, and each
bill opens into a detail drawer. Stdlib only — no `pip install`.

## What it mirrors (and why)

Modelled on how Tipalti's real bill object works (its `CreateOrUpdateInvoices` API and the
Bills workflow), so the demo can answer the SE questions on the customer's email — where the
PO number, line items, and freight/tax are passed, how PO matching / approval / payment are
represented, and how exceptions show. Each bill's drawer shows:

- **Workflow stepper** — AP Review → PO Match → Approval → Payment (Tipalti's `InvoiceStatus`
  path: PendingApReview → PendingApproval → PendingPayment → Paid).
- **Header fields** — Payee (Tipalti's `Idap`), PO number, invoice #, currency, invoice/due
  dates, subsidiary, location, payment method.
- **Invoice lines** — each tagged **Item-based** (a PO line, carrying its
  `RelatedPurchaseOrder.PurchaseOrderNumber`) or **Account-based** (freight and tax — no PO),
  with a GL account and per-line tax. This is where PO lines vs non-PO lines land.
- **Validation & exceptions** — PO present & 3-way matched, amounts within tolerance,
  freight/tax as their own non-PO lines, not a duplicate. Statuses derive automatically: a
  bill with no PO → *Pending AP Review*; totals that don't tie → *On hold — amount tolerance*;
  a repeat payee+invoice → *Duplicate — review*.

Sources: [Tipalti CreateOrUpdateInvoices (SOAP)](https://soap-support.tipalti.com/Content/Topics/PayerAPI/InvoicesAndBills/CreateOrUpdateInvoices/v11.htm) ·
[Tipalti Bills](https://help.tipalti.com/hc/en-us/articles/30710269573655-Bills) ·
[PO matching](https://tipalti.com/ap-automation/po-matching/). The SOAP contract the agent
posts is still our own stub — it only has to match the agent's Tipalti Post code.

## Set it up on another laptop

**Prerequisites**

- **Python 3.8+** (macOS/Linux usually have it; check with `python3 --version`). Nothing to
  `pip install` — the server uses only the standard library.
- **ngrok** — to expose the local server to the agent. Install from
  [ngrok.com/download](https://ngrok.com/download) (or `brew install ngrok` on macOS), then
  authenticate once: `ngrok config add-authtoken <your-token>` (free account is fine).

**Get the code**

```bash
git clone https://github.com/[YOUR_GH_USER]/tipalti-mock-demo.git
cd tipalti-mock-demo
```

## Run it

```bash
python3 app.py          # serves on http://localhost:5055
```

Open **http://localhost:5055/** in a browser — that's the screen to share during the demo.
Leave this terminal running for the whole demo. `Ctrl-C` stops it.

If you see `Address already in use`, a copy is already running — either use it, or free the
port with `lsof -ti tcp:5055 | xargs kill` and start again.

## Expose it to the agent (ngrok)

```bash
ngrok http 5055
```

Copy the `https://<subdomain>.ngrok-free.app` URL and set it in the agent's **Tipalti Post**
tool, in `TIPALTI_MOCK_URL`, **with `/soap` on the end**:

```
TIPALTI_MOCK_URL = "https://<subdomain>.ngrok-free.app/soap"
```

## Endpoints

| Method + path | What it does |
| --- | --- |
| `POST /soap` | Agent posts a SOAP `CreateBill`; a bill is stored and a SOAP response with `<BillId>` is returned. |
| `GET /` | The Tipalti-styled Bills UI (auto-refreshes every 2s). |
| `GET /api/bills` | JSON of stored bills (the UI polls this). |
| `GET /reset` | Clears all bills. **Run this before each demo run** so the queue starts empty. |

## Before every rehearsal / run

Open **http://localhost:5055/reset** (or `curl http://localhost:5055/reset`) to clear the
queue, then reload the UI.

## Notes

- Port is `5055` (macOS uses `5000` for AirPlay). Change `PORT` at the top of `app.py` if needed.
- The SOAP contract (operation `CreateBill`, the field names, the `<BillId>` response) is
  **ours** — a demo stub, not Tipalti's real API. It only has to match the agent's Tipalti
  Post code, which it does.
- Bills are held in memory; restarting the server clears them.
