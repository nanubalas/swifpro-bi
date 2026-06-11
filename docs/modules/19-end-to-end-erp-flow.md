# 19. End-to-end ERP flow

How the whole system connects - from company setup through master data, the buy-side and sell-side transaction
cycles, into the General Ledger, VAT, reporting, and the cross-cutting services (documents, notifications, audit,
import/export, integrations, copilot).

```mermaid
flowchart TD
    subgraph SETUP[Setup & Access]
        CG[Company Group] --> CO[Company / Tenant]
        CO --> COA[Chart of Accounts + Tax Codes seeded]
        CO --> RP[Roles, Permissions & Location Access]
        CO --> ST[Sites -> Locations -> Bins]
    end

    subgraph MASTER[Master Data]
        CU[Customers]
        SU[Suppliers]
        PR[Products / SKUs]
    end
    CO --> MASTER

    subgraph BUY[Purchasing cycle]
        REQ[Requisition] --> PO[Purchase Order]
        PO --> GRN[Goods Receipt]
        GRN --> BILL[Supplier Bill]
        BILL --> SPAY[Supplier Payment]
    end
    SU --> REQ
    PR --> REQ
    LOW[Low-stock reorder] --> REQ

    subgraph SELL[Sales cycle]
        Q[Quote] --> SO[Customer Order]
        SO --> INV[Customer Invoice]
        INV --> RCPT[Customer Receipt]
    end
    CU --> Q
    PR --> Q

    subgraph STOCK[Inventory]
        BAL[Inventory Balance + Movement Ledger]
    end
    GRN -->|receive: +stock| BAL
    INV -->|issue: -stock + COGS| BAL
    ADJ[Stock Adjustments / Transfers / Cycle counts] --> BAL

    subgraph FIN[Finance - General Ledger]
        GL[Journal Entries / GL Accounts]
    end
    INV --> GL
    RCPT --> GL
    BILL --> GL
    SPAY --> GL
    GRN --> GL
    ADJ --> GL
    EXP[Expenses] --> GL
    CN[Credit Notes] --> GL
    ICT[Inter-company sale] --> GL

    GL --> VAT[VAT Return - 9 box / MTD stub]
    GL --> REP[Reports: P&L, Balance Sheet, Aged AR/AP, Cash flow]
    BAL --> REP
    INV --> SREP[Sales reports + Profitability]
    BILL --> SCARD[Supplier scorecard]
    GL --> CONS[Consolidated group reports + eliminations]
    REP --> DASH[Role dashboards / KPIs]

    subgraph XCUT[Cross-cutting services]
        PDF[Documents / PDFs]
        NOTE[Notifications & Alerts]
        AUD[Audit Log - append-only]
        IO[Import / Export CSV/XLSX]
        INTG[Integrations: channels / HMRC stub]
        AI[AI Copilot - proposed]
    end
    INV --> PDF
    PO --> PDF
    INV --> NOTE
    LOW --> NOTE
    SETUP -. every write .-> AUD
    BUY -. every write .-> AUD
    SELL -. every write .-> AUD
    FIN -. every write .-> AUD
    MASTER <--> IO
    INTG --> SO
    REP --> AI
    AI -. read-mostly, audited .-> AUD
```

---

### Appendix - module connectivity matrix

| Module | Feeds into | Reads from |
|---|---|---|
| Company Setup | every module (tenant scope, defaults) | - |
| Roles & Permissions | every module (access control) | Company Setup |
| Customer Mgmt | Sales, Finance (AR), Reports | Company Setup |
| Supplier Mgmt | Purchasing, Finance (AP), Reports | Company Setup |
| Product/SKU | Inventory, Purchasing, Sales | Suppliers, Tax codes |
| Inventory | Sales (COGS), Finance (GL), Reports | Purchasing (GRN), Products, Locations |
| Purchasing | Inventory, Finance (AP), Suppliers, VAT | Requisitions, Products, Suppliers |
| Sales | Inventory, Finance (AR), VAT, Reports | Customers, Products |
| Finance & Accounting | VAT, Reports, Consolidation | Sales, Purchasing, Inventory, Expenses |
| VAT | Reports, HMRC (stub) | Sales, Purchasing, Expenses |
| Expenses | Finance (GL/AP), VAT, Suppliers | Company Setup (threshold) |
| Reports & Dashboards | Copilot | GL, Inventory, Sales, Purchasing |
| Documents/PDFs | Notifications (attachments) | Sales, Purchasing, Finance |
| Notifications | users (email/in-app) | Sales, Inventory, Finance |
| Audit Logs | Reports (compliance) | every module |
| Import/Export | Master data, Finance | every module |
| Integrations | Sales (channels), VAT (HMRC) | Products, Inventory |
| AI Copilot (proposed) | users | Reports, all modules (read) |

---

[← Back to module index](README.md)
