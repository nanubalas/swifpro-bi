"""Seed 'UK Retail Group Ltd' to demonstrate the three-tier ERP separation:

    Company / Organisation   (Tenant - the legal/business entity)
        |
    Site / Branch / Region   (operating & reporting tier)
        |
    Inventory Location       (physical stock storage: warehouse/shop/back-room/...)

It creates:
  - 1 company (GBP, VAT registered)
  - 4 city-branch sites: London (default) + Leicester / Manchester / Birmingham
  - 4 inventory locations under EACH site (16 total)
  - 8 role users, EACH with explicit per-site access (never an "all sites" grant)
  - company-level products (no per-site product rows) + barcodes
  - stock balances keyed by (tenant, location) with site auto-synced from location
  - one of each transaction (sales order, posted invoice, purchase order, goods
    receipt, expense, stock movement), every one stamped with company + site
    (and an inventory location where the document has one)

Idempotent: safe to run repeatedly (`python manage.py seed_uk_retail_demo`).
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from core import roles
from core.models import (
    Tenant, Site, Location, OrgMembership, UserSiteAccess, UserLocationAccess,
    ProductCategory, Product, ProductBarcode, TaxCode, GLAccount,
    Customer, Supplier, CustomerOrder, CustomerOrderLine,
    CustomerInvoice, CustomerInvoiceLine, PurchaseOrder, PurchaseOrderLine,
    GoodsReceipt, GoodsReceiptLine, Expense, InventoryMovement, InventoryBalance,
)
from core.services.inventory import apply_movement
from core.services.gl import post_customer_invoice

PASSWORD = "Demo!2026"

# Site name -> (code, region, address). London is created first and is the default.
SITES = [
    ("London",     "LON", "Greater London", "1 Oxford Street, London, W1D 1AN"),
    ("Leicester",  "LEI", "East Midlands",  "14 Gallowtree Gate, Leicester, LE1 5AD"),
    ("Manchester", "MAN", "North West",     "20 Market Street, Manchester, M1 1WR"),
    ("Birmingham", "BIR", "West Midlands",  "50 New Street, Birmingham, B2 4DU"),
]

# The four inventory locations created under EVERY site (mapped to real Location.Type).
LOC_SPECS = [
    ("Main Warehouse", Location.Type.WAREHOUSE),
    ("Shop Floor",     Location.Type.SHOP_FLOOR),
    ("Back Room",      Location.Type.BACK_ROOM),
    ("Returns Area",   Location.Type.RETURNS),
]

# Role -> the site names that role's user may work in (EXPLICIT; never "all sites").
ROLE_SITES = {
    roles.ADMIN:      ["London", "Leicester", "Manchester", "Birmingham"],
    roles.ACCOUNTANT: ["London", "Leicester", "Manchester", "Birmingham"],
    roles.MANAGER:    ["London", "Leicester"],   # regional manager
    roles.SALES:      ["London"],
    roles.WAREHOUSE:  ["Manchester"],
    roles.PURCHASING: ["Birmingham"],
    roles.FINANCE:    ["London", "Leicester", "Manchester", "Birmingham"],
    roles.READONLY:   ["London", "Manchester"],
}

# sku, name, category, brand, sales_price, standard_cost, barcode
PRODUCTS = [
    ("UKR-001", "LED Desk Lamp",  "Lighting",    "Lumos", "24.99", "9.50", "5012345000018"),
    ("UKR-002", "Wireless Mouse", "Electronics", "Clikr", "14.99", "4.20", "5012345000025"),
]


class Command(BaseCommand):
    help = ("Seed 'UK Retail Group Ltd' with the Company -> Site -> Inventory "
            "Location structure, role users, products, stock and sample transactions.")

    @transaction.atomic
    def handle(self, *args, **options):
        tenant = self._company()
        sites = self._sites(tenant)
        locations = self._locations(tenant, sites)
        users = self._users(tenant, sites, locations)
        std_tax = TaxCode.objects.filter(tenant=tenant, code="STD").first()
        p1, p2 = self._products(tenant, std_tax)
        self._opening_stock(tenant, sites, locations, [p1, p2], users)
        self._transactions(tenant, sites, locations, p1, p2, std_tax, users)
        self._grant_existing_admins(tenant, sites)
        self._summary(tenant)

    # --- Company (legal/business entity) -------------------------------------
    def _company(self):
        tenant, created = Tenant.objects.get_or_create(
            name="UK Retail Group Ltd",
            defaults=dict(
                legal_name="UK Retail Group Limited", trading_name="UK Retail Group",
                business_type="LTD", company_number="09876543",
                vat_registered=True, vat_number="GB123456789",
                address_line1="1 Oxford Street", address_city="London",
                address_postcode="W1D 1AN", address_country="United Kingdom",
                currency_code="GBP", country="United Kingdom",
                timezone="Europe/London", financial_year_start_month=4,
                default_payment_terms_days=30, onboarding_complete=True,
            ),
        )
        self.stdout.write(("Created" if created else "Reusing") + f" company: {tenant.name}")
        return tenant

    # --- Sites (operating/reporting tier; London is the default) -------------
    def _sites(self, tenant):
        sites = {}
        # The post_save signal already made a default "Main Site"; repurpose it as London.
        london = Site.objects.filter(tenant=tenant, is_default=True).first() \
            or Site(tenant=tenant, is_default=True)
        _, code, region, addr = SITES[0]
        london.name = "London"
        london.code = code
        london.site_type = Site.Type.CITY_BRANCH
        london.region = region
        london.address = addr
        london.is_default = True
        london.is_active = True
        london.save()
        sites["London"] = london
        # Drop the auto "Main Location" so each site has exactly its four locations.
        Location.objects.filter(tenant=tenant, name="Main Location").delete()

        for name, code, region, addr in SITES[1:]:
            s, _ = Site.objects.get_or_create(
                tenant=tenant, name=name,
                defaults=dict(code=code, site_type=Site.Type.CITY_BRANCH,
                              region=region, address=addr, is_default=False, is_active=True),
            )
            sites[name] = s
        self.stdout.write(f"  Sites: {', '.join(sites)} (default: London)")
        return sites

    # --- Inventory locations (physical stock storage; 4 per site) -----------
    def _locations(self, tenant, sites):
        locations = {}
        for site_name, site in sites.items():
            for label, ltype in LOC_SPECS:
                loc, _ = Location.objects.get_or_create(
                    tenant=tenant, name=f"{site_name} {label}",   # name unique per tenant
                    defaults=dict(site=site, type=ltype, is_active=True, holds_stock=True),
                )
                if loc.site_id != site.id:          # pin to its own site (never re-homed)
                    loc.site = site
                    loc.save(update_fields=["site"])
                locations[(site_name, label)] = loc
        self.stdout.write(f"  Inventory locations: {len(locations)} ({len(LOC_SPECS)} per site)")
        return locations

    # --- Users (one per role; explicit per-site access; NO "all sites") -----
    def _users(self, tenant, sites, locations):
        users = {}
        for role, site_names in ROLE_SITES.items():
            uname = f"{role.lower()}@ukretail.demo"
            u, created = User.objects.get_or_create(
                username=uname,
                defaults=dict(email=uname, is_active=True,
                              first_name=roles.ROLE_LABELS.get(role, role)),
            )
            if created:
                u.set_password(PASSWORD)
                u.save()
            OrgMembership.objects.get_or_create(
                user=u, tenant=tenant, defaults=dict(role=role, is_default=True))
            for sn in site_names:                    # explicit, concrete site grants
                UserSiteAccess.objects.get_or_create(tenant=tenant, user=u, site=sites[sn])
            users[role] = u
        # Warehouse staff is further restricted to ONE inventory location (Manchester WH).
        UserLocationAccess.objects.get_or_create(
            tenant=tenant, user=users[roles.WAREHOUSE],
            location=locations[("Manchester", "Main Warehouse")])
        self.stdout.write(f"  Users: {len(users)} (each with explicit UserSiteAccess)")
        return users

    # --- Make the data visible to existing admins / superusers --------------
    def _grant_existing_admins(self, tenant, sites):
        """Give existing superusers (and any user named 'admin') membership +
        full site access to this company, so the seeded data is browsable in the
        app right after seeding - no need to use the demo logins. Their existing
        default company is left unchanged; they can switch to UK Retail Group via
        the dashboard workspace switcher."""
        from django.db.models import Q
        admins = list(User.objects.filter(Q(is_superuser=True) | Q(username="admin")).distinct())
        for u in admins:
            OrgMembership.objects.get_or_create(
                user=u, tenant=tenant, defaults=dict(role=roles.ADMIN))
            for s in sites.values():
                UserSiteAccess.objects.get_or_create(tenant=tenant, user=u, site=s)
        if admins:
            self.stdout.write(
                f"  Granted {len(admins)} existing admin/superuser(s) access to {tenant.name}: "
                + ", ".join(u.username for u in admins))

    # --- Products (company level - Product has NO site FK) ------------------
    def _products(self, tenant, std_tax):
        cats = {
            "Electronics": ProductCategory.objects.get_or_create(tenant=tenant, name="Electronics")[0],
            "Lighting": ProductCategory.objects.get_or_create(tenant=tenant, name="Lighting")[0],
        }
        made = []
        for sku, name, cat, brand, price, cost, barcode in PRODUCTS:
            p, _ = Product.objects.get_or_create(
                tenant=tenant, sku=sku,
                defaults=dict(name=name, product_type=Product.Type.STOCK, category=cats[cat],
                              brand=brand, sales_price=Decimal(price), standard_cost=Decimal(cost),
                              cost_method=Product.CostMethod.AVERAGE, tax_code=std_tax,
                              reorder_level=Decimal("20"), uom="each", is_active=True),
            )
            ProductBarcode.objects.get_or_create(tenant=tenant, code=barcode, defaults=dict(product=p))
            made.append(p)
        self.stdout.write(f"  Products: {len(made)} (company-level, no per-site rows)")
        return made[0], made[1]

    # --- Opening stock at each site's Main Warehouse ------------------------
    def _opening_stock(self, tenant, sites, locations, products, users):
        if InventoryMovement.objects.filter(tenant=tenant, ref_type="OPENING").exists():
            return  # already seeded
        for site_name in sites:
            wh = locations[(site_name, "Main Warehouse")]
            for prod in products:
                apply_movement(
                    tenant=tenant, product=prod, location=wh,
                    movement_type=InventoryMovement.MovementType.RECEIVE,
                    qty_delta=Decimal("100"), ref_type="OPENING", ref_id="SEED",
                    unit_cost=prod.standard_cost, user=users[roles.WAREHOUSE])
        self.stdout.write("  Opening stock: 100 units of each product at every Main Warehouse")

    # --- Sample transactions (company + site stamped; +location where applicable) ---
    def _transactions(self, tenant, sites, locations, p1, p2, std_tax, users):
        lon_wh = locations[("London", "Main Warehouse")]
        cust, _ = Customer.objects.get_or_create(tenant=tenant, name="Acme Retail Customer")
        supp, _ = Supplier.objects.get_or_create(tenant=tenant, name="Globex Supplies Ltd")

        # (a) Sales order - site auto-derived from location.site (London)
        so, created = CustomerOrder.objects.get_or_create(
            tenant=tenant, order_number="SO-0001",
            defaults=dict(customer=cust, location=lon_wh, order_date=date.today(),
                          status=CustomerOrder.Status.CONFIRMED))
        if created:
            CustomerOrderLine.objects.create(order=so, product=p1, qty=Decimal("5"),
                                             unit_price=p1.sales_price, tax_code=std_tax)

        # (b) Customer invoice (London) -> posted: AR/Revenue/VAT + COGS, -5 stock
        inv, created = CustomerInvoice.objects.get_or_create(
            tenant=tenant, invoice_number="INV-0001",
            defaults=dict(customer=cust, location=lon_wh, source_order=so,
                          invoice_date=date.today(), status=CustomerInvoice.Status.DRAFT))
        if created:
            CustomerInvoiceLine.objects.create(invoice=inv, product=p1, qty=Decimal("5"),
                                               unit_price=p1.sales_price, tax_code=std_tax)
            post_customer_invoice(inv, user=users[roles.ACCOUNTANT])

        # (c) Purchase order - site auto-derived from receiving_location.site (London)
        po, created = PurchaseOrder.objects.get_or_create(
            tenant=tenant, po_number="PO-0001",
            defaults=dict(supplier=supp, receiving_location=lon_wh,
                          status=PurchaseOrder.Status.APPROVED))
        po_line = po.lines.first()
        if created and po_line is None:
            po_line = PurchaseOrderLine.objects.create(
                po=po, product=p2, ordered_qty=Decimal("50"), unit_cost=p2.standard_cost,
                tax_code=std_tax)

        # (d) Goods receipt - received_to is the inventory location (London Main Warehouse)
        grn, created = GoodsReceipt.objects.get_or_create(
            tenant=tenant, grn_number="GRN-0001",
            defaults=dict(po=po, received_to=lon_wh, status=GoodsReceipt.Status.DRAFT))
        if created and po_line is not None:
            GoodsReceiptLine.objects.create(receipt=grn, po_line=po_line, product=p2,
                                            qty_received=Decimal("50"), unit_cost=p2.standard_cost)

        # (e) Expense - no location; site defaults to the company default site (London)
        exp_acct = (GLAccount.objects.filter(tenant=tenant, type=GLAccount.Type.EXPENSE).order_by("id").first()
                    or GLAccount.objects.filter(tenant=tenant).order_by("id").first())
        Expense.objects.get_or_create(
            tenant=tenant, reference="EXP-0001",
            defaults=dict(payee="Royal Mail", category=exp_acct, net_amount=Decimal("40.00"),
                          tax_code=std_tax, paid=True, method=Expense.Method.BANK,
                          expense_date=date.today(), status=Expense.Status.DRAFT))

        # (f) Stock movement - a manual adjustment at London Main Warehouse
        if not InventoryMovement.objects.filter(tenant=tenant, ref_id="SEED-ADJ").exists():
            apply_movement(
                tenant=tenant, product=p1, location=lon_wh,
                movement_type=InventoryMovement.MovementType.ADJUSTMENT,
                qty_delta=Decimal("-2"), ref_type="MANUAL", ref_id="SEED-ADJ",
                unit_cost=p1.standard_cost, user=users[roles.WAREHOUSE])
        self.stdout.write("  Transactions: SO-0001, INV-0001 (posted), PO-0001, GRN-0001, expense, stock adjustment")

    # --- Summary -------------------------------------------------------------
    def _summary(self, tenant):
        self.stdout.write(self.style.SUCCESS(
            f"UK Retail demo ready: company '{tenant.name}', "
            f"{Site.objects.filter(tenant=tenant).count()} sites, "
            f"{Location.objects.filter(tenant=tenant).count()} inventory locations, "
            f"{OrgMembership.objects.filter(tenant=tenant).count()} users, "
            f"{Product.objects.filter(tenant=tenant).count()} products, "
            f"{InventoryBalance.objects.filter(tenant=tenant).count()} stock balances."))
